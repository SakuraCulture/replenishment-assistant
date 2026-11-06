"""AI inventory queries use the exact same bounded read-only service as the UI."""
from datetime import datetime
from pathlib import Path
from automation_protocol import CommandError
from automation_queries import page
import inventory_structure as service


def require(gateway,task):
    report=getattr(gateway,'inventory_reports',{}).get(task)
    if not report:raise CommandError('RESULT_UNAVAILABLE','货盘结果已失效或未完成，请重新查询')
    try:service.validate_basis(gateway.host.work_repository.path,report)
    except ValueError as e:raise CommandError('STALE_CONTEXT',str(e)) from None
    return report


def query(gateway,args):
    key='inventory:'+args['operation_key'];existing=gateway.journal.lookup(key,args)
    if existing:return gateway.task_record(existing['task_id'])
    gateway.idle();gateway.check_context(args['context_token']);spec=service.query_spec(args['query'])
    task,_=gateway.journal.create(key,args,'inventory')
    def finish(report):
        if gateway.worker.cancel.is_set():gateway.journal.update(task['task_id'],state='stopped',message='货盘查询已停止');return
        if not hasattr(gateway,'inventory_reports'):gateway.inventory_reports={}
        gateway.inventory_reports[task['task_id']]=report
        while len(gateway.inventory_reports)>2:gateway.inventory_reports.pop(next(iter(gateway.inventory_reports)))
        gateway.journal.update(task['task_id'],state='completed',snapshot_id=report['snapshot_id'],message='货盘查询完成，请分页读取',round_count=len(report['rounds']))
    gateway.start_job(task,lambda w:service.collect(gateway.host.work_repository.path,spec,w.progress.emit,w.cancel),finish)
    return task


def results(gateway,args):
    report=require(gateway,args['task_id']);level=args.get('level','rounds')
    if level=='detail' and 'details' not in report:raise CommandError('NEEDS_REVIEW','请用新的operation_key查询并同时指定detail_store、detail_day，后台加载所选轮次明细')
    rows=report['details'] if level=='detail' else report['rounds']
    if args.get('store'):rows=[r for r in rows if r['store']==args['store']]
    return {'metric_version':report['metric_version'],'snapshot_id':report['snapshot_id'],'as_of':report['as_of'],'query':report['query'],
            'overview':report['overview'],'definitions':report['definitions'],'page':page(rows,args.get('offset',0),args.get('limit',50))}


def export(gateway,args):
    import export_settings
    import inventory_structure_exports as exports
    key='inventory-export:'+args['operation_key'];existing=gateway.journal.lookup(key,args)
    if existing:return gateway.task_record(existing['task_id'])
    gateway.idle();gateway.check_context(args['context_token']);report=require(gateway,args['task_id'])
    if report['snapshot_id']!=args['snapshot_id']:raise CommandError('STALE_CONTEXT','货盘快照不一致')
    directory=args.get('directory') or export_settings.load(Path(gateway.host.repository.folder)/export_settings.FILENAME)
    if not directory:raise CommandError('NEEDS_REVIEW','请设置默认导出位置或指定本地导出目录')
    target=Path(export_settings.validate(directory,True))/('货盘结构分析_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.xlsx')
    task,_=gateway.journal.create(key,args,'inventory_export')
    def finish(receipt):gateway.journal.update(task['task_id'],state='completed',message='货盘报表已导出，业务数据未变',receipt=receipt)
    gateway.start_job(task,lambda w:exports.export(gateway.host.work_repository.path,report,target,args.get('include_details',False),w.progress.emit,w.cancel),finish)
    return task
