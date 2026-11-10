"""One-shot analysis worker. Imported lazily; never creates a Qt application."""


def serve(connection,folder,operation,arguments,cancel):
    import traceback
    import sys
    import tempfile
    import os
    from stockout_common import task_lock
    from stockout_repository import Repository
    from runtime_support import checkpoint
    repo=Repository(folder)
    def progress(message):connection.send(('progress',str(message)))
    def rpc(service,**payload):
        connection.send(('rpc',{'service':service,**payload}))
        kind,value=connection.recv()
        if kind!='rpc_result':raise RuntimeError(str(value))
        return value
    try:
        with task_lock(repo.folder):
            checkpoint(cancel)
            temp_root=repo.folder/'temporary';temp_root.mkdir(exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='task-',dir=temp_root) as temporary:
                tempfile.tempdir=temporary
                for key in ('SQLITE_TMPDIR','TEMP','TMP'):os.environ[key]=temporary
                repo.initialize()
                result=execute(repo,operation,arguments,cancel,progress,rpc)
            connection.send(('diagnostic',{'pid':os.getpid(),'qt_loaded':any(k.startswith('PySide6') for k in sys.modules),
                                          'operation':operation}))
        connection.send(('success',result))
    except Exception as exc:
        import sqlite3
        message=str(exc)
        if isinstance(exc,sqlite3.DatabaseError):
            message=('已安全取消查询；已留存日报保持不变' if getattr(exc,'sqlite_errorcode',None)==sqlite3.SQLITE_INTERRUPT else
                     '分析库本次读写未完成。请保留分析资料并查看应用日志；原补货可以继续使用')
        connection.send(('error',{'type':type(exc).__name__,'message':message,'traceback':traceback.format_exc()}))
    finally:
        connection.close()


def execute(repo,operation,args,cancel,progress,rpc):
    import stockout_metrics as metrics
    from runtime_support import checkpoint
    def public_settings(value):
        if value is None:return None
        profile=value.get('profile')
        return {**{k:v for k,v in value.items() if k!='profile'},
                'profile':{k:v for k,v in profile.items() if k!='items'}|{'count':len(profile['items'])} if profile else None}
    if operation=='open':
        import shutil
        from stockout_views import load,defaults
        try:views=load(repo);views_error=''
        except (ValueError,OSError) as exc:views=defaults();views_error='切面配置未读取：'+str(exc)
        with repo.connect() as db:
            originals=db.execute('SELECT coalesce(sum(bytes),0) FROM originals').fetchone()[0]
            cleanups=[dict(r) for r in db.execute("SELECT id,created FROM cleanups WHERE state='FILES_PENDING' ORDER BY created DESC LIMIT 10")]
        return {'views':views,'views_error':views_error,'settings':public_settings(repo.settings()),'runs':repo.recent(include_result_links=args.get('include_result_links',False)),'dates':metrics.dates(repo),
                'space':{'database':repo.path.stat().st_size,'originals':originals,'free':shutil.disk_usage(repo.folder).free},'cleanups':cleanups}
    if operation=='choices':
        from stockout_settings import supply_snapshot
        config=rpc('ax',action='configuration',values={})
        stores=[{'code':s['buyerWarehouseCode'],'name':s['displayName']} for s in config.get('targets',[])]
        return {'stores':stores, **supply_snapshot(repo.root,stores),
                'configured':bool(config.get('configured') and config.get('enabled'))}
    if operation=='adopt':
        return public_settings(repo.adopt_business(args['stores'],use_builtin=bool(args.get('use_builtin'))))
    if operation=='query':
        options=args['query']
        value=metrics.query(repo,options,cancel=cancel)
        value['cuts']=metrics.cuts(repo,options,tuple(args.get('dimensions',['store'])),offset=args.get('cut_offset',0),cancel=cancel)
        value['details']=metrics.detail(repo,options,offset=args.get('offset',0),only_zero=args.get('only_zero',False),
                                       excluded=args.get('excluded',False),sku=args.get('sku',''),cancel=cancel)
        value['trend']=metrics.trend(repo,options,cancel=cancel)
        return value
    if operation=='export':
        from stockout_exports import export
        return export(repo,args['query'],args['destination'],include_detail=args.get('include_detail',False),
                      dimensions=args.get('dimensions',['store']),combinations=args.get('combinations'),cancel=cancel,progress=progress)
    if operation=='filter_values':
        return metrics.filter_values(repo,args['query'],args['dimension'],search=args.get('search',''),offset=args.get('offset',0),cancel=cancel)
    if operation=='save_views':
        from stockout_views import save
        return save(repo,args['views'],args['expected'])
    if operation=='collect':
        from stockout_acquisition import execute
        rid=args.get('resume')
        if not rid:
            if args.get('trigger','').startswith('schedule:'):
                with repo.connect() as db:
                    prior=db.execute('SELECT id FROM runs WHERE trigger=?',(args['trigger'],)).fetchone()
                if prior:return repo.run(prior[0])  # exactly one intention per scheduled day
            run=repo.new_run(trigger=args.get('trigger','manual'),source_run=args.get('source_run'));rid=run['id']
        progress('已建立分析任务 '+rid)
        return execute(repo,rid,rpc,cancel,progress)
    if operation=='end':
        from stockout_acquisition import finish_local
        return finish_local(repo,args['run'],rpc,cancel)
    if operation=='reexport_preview':
        from stockout_acquisition import reexport_preview
        return reexport_preview(repo,args['run'],args['codes'],rpc,cancel,progress)
    if operation=='reexport':
        from stockout_acquisition import reexport
        return reexport(repo,args['plan'],rpc,cancel,progress)
    if operation=='run':return repo.run(args['run'])
    if operation=='run_action_preview':
        from stockout_lifecycle import preview
        return preview(repo,args['run'],args['action'],args.get('mode','current'))
    if operation=='delete_run_log':
        from stockout_lifecycle import delete_log
        return delete_log(repo,args['plan'],cancel)
    if operation=='rerun':
        from stockout_lifecycle import rerun
        return rerun(repo,args['plan'],rpc,cancel,progress)
    if operation=='recalculate':
        from stockout_lifecycle import recalculate
        return recalculate(repo,args.get('plan'),resume=args.get('resume'),cancel=cancel,progress=progress)
    if operation=='cleanup_preview':
        from stockout_maintenance import preview
        return preview(repo,args['start'],args['end'])
    if operation=='cleanup':
        from stockout_maintenance import execute
        return execute(repo,args['plan'],cancel,progress)
    if operation=='cleanup_retry':
        from stockout_maintenance import finish_files
        return finish_files(repo,args['id'],progress)
    if operation=='ingest':
        # Offline acceptance uses the same projection/publish path with explicit original evidence.
        from stockout_facts import load_catalog,load_store
        rid=repo.new_run(day=args.get('day'))['id'];catalogs={}
        for warehouse,item in args.get('catalogs',{}).items():
            checkpoint(cancel)
            catalogs[warehouse]=load_catalog(repo,item['file'],warehouse,expected=item['sha256'],expected_rows=item['rows'],cancel=cancel,progress=progress)
        repo.change(rid,catalogs=catalogs)
        for item in args['files']:
            load_store(repo,rid,item['store'],item['file'],observed=item['observed'],expected=item['sha256'],expected_rows=item['rows'],cancel=cancel,progress=progress)
        return repo.publish(rid)
    raise ValueError('未知分析操作')
