"""Lazy one-shot warehouse worker; shared transport, warehouse-only state."""
import json,os,tempfile,traceback
from pathlib import Path
from stockout_common import task_lock,today,file_hash
from runtime_support import checkpoint,Cancelled
import warehouse_stockout as service

def serve(connection,folder,operation,args,cancel):
    repo=service.Repository(folder)
    def progress(message):connection.send(('progress',str(message)))
    def rpc(name,**payload):
        connection.send(('rpc',{'service':name,**payload}));kind,value=connection.recv()
        if kind!='rpc_result':raise RuntimeError(str(value))
        return value
    try:
        with task_lock(repo.folder):
            repo.initialize();root=repo.folder/'temporary';root.mkdir(exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='task-',dir=root) as temp:
                tempfile.tempdir=temp
                for key in ('SQLITE_TMPDIR','TEMP','TMP'):os.environ[key]=temp
                result=execute(repo,operation,args,cancel,progress,rpc)
        import sys
        connection.send(('diagnostic',{'pid':os.getpid(),'qt_loaded':any(k.startswith('PySide6') for k in sys.modules),'operation':operation}))
        connection.send(('success',result))
    except Exception as exc:connection.send(('error',{'type':type(exc).__name__,'message':str(exc),'traceback':traceback.format_exc()}))
    finally:connection.close()

def execute(repo,operation,args,cancel,progress,rpc):
    import warehouse_stockout_views as views
    from warehouse_stockout_maintenance import preview,remove,cleanup,delete_log
    checkpoint(cancel)
    if operation=='open':
        with repo.connect() as db:
            runs=[dict(row) for row in db.execute('SELECT id,day,basis,state,message,created,updated,source_run FROM runs WHERE log_deleted=0 ORDER BY rowid DESC LIMIT 100')]
            pending=db.execute('SELECT count(*) FROM cleanup').fetchone()[0]
        return {'selected':repo.selected(),'runs':runs,'views':views.load(repo),'pending_cleanup':pending,'database_bytes':repo.path.stat().st_size}
    if operation=='select':return repo.select(args['warehouses'])
    if operation=='query':return service.query(repo,args)
    if operation=='cuts':return service.cuts(repo,args['options'],args['dimensions'],offset=args.get('offset',0),cancel=cancel)
    if operation=='details':return service.detail(repo,args['options'],offset=args.get('offset',0),cancel=cancel)
    if operation=='values':return service.filter_values(repo,args['options'],args['dimension'],args.get('search',''))
    if operation=='save_views':return views.save(repo,args['views'],args['expected'])
    if operation=='export':
        from warehouse_stockout_exports import export
        return export(repo,args['options'],args['destination'],args['combinations'],include_detail=args.get('include_detail',True),detail_mode=args.get('detail_mode'),cancel=cancel,progress=progress)
    if operation=='delete_preview':return preview(repo,args['start'],args['end'])
    if operation=='cleanup':return remove(repo,args['plan'],cancel)
    if operation=='cleanup_retry':return cleanup(repo,cancel)
    if operation=='delete_run_log':return delete_log(repo,args['run'],args['updated'])
    if operation=='end':repo.change(args['run'],state='ENDED',message='已结束本地准备；平台原导出任务不会撤销');return True
    if operation not in ('collect','ingest','recalculate','rerun'):raise ValueError('未知大仓分析操作')
    source=repo.run(args['run']) if args.get('run') else None
    if operation=='collect' and source:
        if source['day']!=today():raise ValueError('原作业已跨日；请结束原准备后采集今日，不能用今日库存补历史')
        rid=source['id'];warehouses=json.loads(source['basis'])['warehouses']
    else:
        warehouses=json.loads(source['basis'])['warehouses'] if source else args.get('warehouses',repo.selected())
        if operation=='ingest':warehouses=[args['warehouse']]
        rid=repo.new_run(warehouses,source=source['id'] if source else None,day=source['day'] if operation=='recalculate' else None)
    try:
        repo.change(rid,state='RUNNING')
        if operation=='ingest':
            service.load_master(repo,args['path'],args['warehouse'],run=rid,cancel=cancel,progress=progress)
        elif operation=='recalculate':
            with repo.connect() as db:
                snapshots=[dict(r) for r in db.execute('SELECT s.*,o.path FROM run_snapshots r JOIN snapshots s ON s.id=r.snapshot JOIN originals o ON o.sha=s.source_sha WHERE r.run=?',(source['id'],))]
            if not snapshots:raise ValueError('原分析数据已删除或原件缺失，不能重算')
            for row in snapshots:
                target=Path(row['path']).resolve()
                if target.parent!=(repo.folder/'originals').resolve() or file_hash(target)!=row['source_sha']:raise ValueError('原件位置或完整性校验失败')
                service.load_master(repo,target,row['warehouse'],run=rid,expected=row['source_sha'],expected_rows=row['rows'],observed=row['observed'],cancel=cancel,progress=progress)
        else:
            from stockout_acquisition import collect_catalogs
            def loader(repo,path,warehouse,**values):return service.load_master(repo,path,warehouse,run=rid,**values)
            collect_catalogs(repo,rid,rpc,cancel,progress,warehouses=warehouses,loader=loader)
        checkpoint(cancel)
        with repo.connect() as db:ready=[r[0] for r in db.execute('SELECT warehouse FROM run_snapshots WHERE run=?',(rid,))]
        for wh in ready:checkpoint(cancel);repo.publish(rid,wh)
        repo.change(rid,state='COMPLETE' if len(ready)==len(warehouses) else 'PARTIAL',message=f'已留存{len(ready)}/{len(warehouses)}仓；未完成仓不补0')
        return {'run':rid,'ready':ready}
    except Exception as exc:
        repo.change(rid,state='PAUSED' if isinstance(exc,Cancelled) else 'PARTIAL',message=str(exc));raise
