"""Narrow AI adapters over the same native analytics services."""
from datetime import datetime
from pathlib import Path
from automation_protocol import CommandError
import order_analytics as service


def require(gateway,task):
    reports=getattr(gateway,'analytics_reports',{})
    if task not in reports:raise CommandError('RESULT_UNAVAILABLE','分析结果不在当前会话，请重新查询；不会读取旧缓存冒充最新结果')
    report=reports[task]
    try:service.validate_basis(gateway.host.work_repository.path,report)
    except ValueError as exc:raise CommandError('STALE_CONTEXT',str(exc)) from None
    return report


def query(gateway,args):
    key='analytics:'+args['operation_key'];existing=gateway.journal.lookup(key,args)
    if existing:return gateway.task_record(existing['task_id'])
    gateway.idle();gateway.check_context(args['context_token']);spec=service.query_spec(args['query'])
    task,_=gateway.journal.create(key,args,'analytics')
    def finish(report):
        if gateway.worker.cancel.is_set():
            gateway.journal.update(task['task_id'],state='stopped',message='分析已停止，未发布部分结果');return
        if not hasattr(gateway,'analytics_reports'):gateway.analytics_reports={}
        gateway.analytics_reports[task['task_id']]=report
        while len(gateway.analytics_reports)>3:gateway.analytics_reports.pop(next(iter(gateway.analytics_reports)))
        gateway.journal.update(task['task_id'],state='completed',snapshot_id=report['snapshot_id'],
            message='经营分析完成，请分页读取结果',row_count=len(report['rows']),order_count=len(report['heads']),
            as_of=report['as_of'],scope='只读分析；原业务未变')
    gateway.start_job(task,lambda w:service.collect(gateway.host.work_repository.path,spec,w.progress.emit,w.cancel),finish)
    return task


def results(gateway,args):
    from automation_queries import page
    report=require(gateway,args['task_id']);level=args.get('level','store')
    rows=report[{'store':'stores','warehouse':'warehouses','order':'orders'}.get(level,'rows')]
    if level=='detail':rows=[r for r in rows if r['scope']=='纳入']
    elif level=='pending':rows=[r for r in rows if r['scope']=='待核']
    if args.get('store'):rows=[r for r in rows if r['store']==args['store']]
    if args.get('order'):rows=[r for r in rows if r.get('order')==args['order']]
    return {'snapshot_id':report['snapshot_id'],'as_of':report['as_of'],'query':report['query'],
            'metrics':report['metrics'],'groups':report['groups'],
            'definitions':{'model':'自动建议，含启用的一箱例外，总预算和人工之前','payment':'业务确认有效的付款数量；本地生成不算实际下单',
                'price':'各段统一签发价；实际付款金额另列','missing':'未导入或待重新核对不算0','group':'当时必卖清单；无依据为属性待核',
                'scope':'各段货值只含已知部分；比率与差额使用相同可比集合'},
            'page':page(rows,args.get('offset',0),args.get('limit',50))}


def export(gateway,args):
    import export_settings
    import order_analytics_exports
    key='analytics-export:'+args['operation_key'];existing=gateway.journal.lookup(key,args)
    if existing:return gateway.task_record(existing['task_id'])
    gateway.idle();gateway.check_context(args['context_token']);report=require(gateway,args['task_id'])
    if report['snapshot_id']!=args['snapshot_id']:raise CommandError('STALE_CONTEXT','指定的查询快照不一致')
    directory=args.get('directory') or export_settings.load(Path(gateway.host.repository.folder)/export_settings.FILENAME)
    if not directory:raise CommandError('NEEDS_REVIEW','请先设置默认导出目录，或明确提供本地导出目录')
    directory=export_settings.validate(directory,True)
    target=Path(directory)/('补货经营分析_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.xlsx')
    task,_=gateway.journal.create(key,args,'analytics_export')
    def finish(receipt):gateway.journal.update(task['task_id'],state='completed',message='报表已导出，业务数据未变',receipt=receipt)
    gateway.start_job(task,lambda w:order_analytics_exports.export(gateway.host.work_repository.path,report,target,
        args.get('level','store'),args.get('orders'),w.progress.emit,w.cancel),finish)
    return task
