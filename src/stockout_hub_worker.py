"""One-shot hub worker; no database initialization during ordinary app startup."""
def serve(connection,folder,operation,args,cancel):
    import os,tempfile,traceback
    from pathlib import Path
    from contextlib import ExitStack
    from stockout_common import task_lock
    from stockout_hub_service import Journal,preview,create,execute,end
    repo=Journal(folder)
    def progress(message):connection.send(('progress',str(message)))
    def rpc(service,**payload):
        connection.send(('rpc',{'service':service,**payload}));kind,value=connection.recv()
        if kind!='rpc_result':raise RuntimeError(str(value))
        return value
    try:
        with ExitStack() as locks:
            locks.enter_context(task_lock(repo.folder))
            if operation in ('hub_collect','hub_scheduled','hub_end','hub_export','hub_reuse'):
                locks.enter_context(task_lock(Path(folder)/'analysis'/'stockout'))
                locks.enter_context(task_lock(Path(folder)/'analysis'/'warehouse_stockout'))
            tmp=repo.folder/'temporary';tmp.mkdir(exist_ok=True)
            with tempfile.TemporaryDirectory(dir=tmp,prefix='job-') as temp:
                tempfile.tempdir=temp
                for key in ('SQLITE_TMPDIR','TEMP','TMP'):os.environ[key]=temp
                if operation=='hub_open':
                    from stockout_repository import Repository
                    from warehouse_stockout import Repository as Warehouse
                    store=Repository(folder);warehouse=Warehouse(folder)
                    settings=store.settings() if store.path.exists() else None
                    result={'runs':repo.recent(),'stores':(settings or {}).get('stores',[]),'warehouses':warehouse.selected()}
                elif operation=='hub_preview':result=preview(folder,args['mode'],args.get('warehouses'))
                elif operation in ('hub_collect','hub_scheduled'):
                    rid=args.get('resume')
                    if not rid:
                        plan=args.get('plan') or preview(folder,args['mode'],args.get('warehouses'))
                        rid=create(repo,plan,trigger=args.get('trigger','manual'))['id']
                    progress('统一分析原作业 '+rid);result=execute(repo,rid,rpc,cancel,progress)
                elif operation=='hub_end':result=end(repo,args['run'],rpc,cancel)
                elif operation=='hub_export':
                    from stockout_hub_exports import export
                    result=export(folder,args,cancel,progress)
                elif operation=='hub_reuse':
                    from stockout_hub_reuse import reuse
                    result=reuse(repo,args['run'],cancel,progress)
                else:raise ValueError('未知缺货率操作')
        import sys
        connection.send(('diagnostic',{'pid':os.getpid(),'qt_loaded':any(k.startswith('PySide6') for k in sys.modules),'operation':operation}))
        connection.send(('success',result))
    except Exception as exc:connection.send(('error',{'type':type(exc).__name__,'message':str(exc),'traceback':traceback.format_exc()}))
    finally:connection.close()
