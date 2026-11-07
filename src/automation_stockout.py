"""Lazy, bounded AI adapter for the independent stockout service."""
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from automation_protocol import CommandError, digest, wire_default


def folder(gateway):return gateway.host.repository.folder


def expire(gateway):
    reports=getattr(gateway,'stockout_reports',{})
    for key in list(reports):
        if reports[key]['basis']['expires_at']<=time.time():reports.pop(key,None)


def require(gateway,task):
    import stockout_ai as service
    expire(gateway);report=getattr(gateway,'stockout_reports',{}).get(task)
    if not report:raise CommandError('RESULT_UNAVAILABLE','分析会话未完成、已过期或应用已重启，请重新查询；未改用新数据')
    try:
        with service.PinnedRepository(folder(gateway),report['basis']).connect():pass
    except ValueError as exc:
        gateway.stockout_reports.pop(task,None)
        raise CommandError('STALE_CONTEXT',str(exc)) from None
    return report


def context(gateway,args):
    import stockout_ai as service
    return service.context(folder(gateway),args.get('offset',0),args.get('limit',100))


def query(gateway,args):
    import stockout_ai as service
    key='stockout:'+args['operation_key'];old=gateway.journal.lookup(key,args)
    if old:return gateway.task_record(old['task_id'])
    gateway.idle();gateway.check_context(args['context_token']);spec=service.options(args['query'])
    dimensions=service.metrics.dimension_keys(args.get('dimensions',['store']))
    task,_=gateway.journal.create(key,args,'stockout_analysis')
    def finish(report):
        if gateway.worker.cancel.is_set():gateway.journal.update(task['task_id'],state='stopped',message='分析已停止，原资料未变');return
        if not hasattr(gateway,'stockout_reports'):gateway.stockout_reports={}
        expire(gateway);report['pages']={};report['pending_pages']={}
        gateway.stockout_reports[task['task_id']]=report
        while len(gateway.stockout_reports)>3:gateway.stockout_reports.pop(next(iter(gateway.stockout_reports)))
        if not hasattr(gateway,'stockout_expiry'):
            from PySide6.QtCore import QTimer
            gateway.stockout_expiry=QTimer(gateway);gateway.stockout_expiry.setInterval(60000)
            gateway.stockout_expiry.timeout.connect(lambda:expire(gateway))
        gateway.stockout_expiry.start()
        gateway.journal.update(task['task_id'],state='completed',snapshot_id=report['snapshot_id'],message='固定日报分析完成，请读取结果；未采日期不补0')
    gateway.start_job(task,lambda w:service.analyze(folder(gateway),spec,dimensions,w.progress.emit,w.cancel),finish)
    return task


def envelope(report):
    return {**{k:report[k] for k in ('snapshot_id','overview','trend','comparison','rules')},
            'query':report['basis']['query'],'publications':report['basis']['publications'],
            'expires_at':report['basis']['expires_at']}


def results(gateway,args,operation='results'):
    import stockout_ai as service
    report=require(gateway,args['task_id'])
    if operation=='results' and args.get('level','summary')=='summary':return envelope(report)
    key=digest({'operation':operation,**args})
    if key in report['pages']:return {'snapshot_id':report['snapshot_id'],**report['pages'][key]}
    pending=report['pending_pages'].get(key)
    if pending and gateway.task_record(pending)['state'] not in ('completed','failed','stopped'):
        return {'page_pending':True,'page_task_id':pending,'analysis_task_id':args['task_id']}
    gateway.idle()
    task,_=gateway.journal.create('stockout-page:'+uuid.uuid4().hex,args,'stockout_page')
    report['pending_pages'][key]=task['task_id']
    while len(report['pending_pages'])>16:report['pending_pages'].pop(next(iter(report['pending_pages'])))
    def work(w):
        if operation=='filters':
            return service.metrics.filter_values(service.PinnedRepository(folder(gateway),report['basis']),report['basis']['query'],
                args['dimension'],search=args.get('search',''),offset=args.get('offset',0),limit=args.get('limit',100),cancel=w.cancel)
        return service.read(folder(gateway),report,'evidence' if operation=='evidence' else args['level'],args.get('dimensions'),
            args.get('offset',0),args.get('limit',100),args.get('store',''),args.get('sku',''),w.cancel)
    def finish(page):
        if gateway.worker.cancel.is_set():gateway.journal.update(task['task_id'],state='stopped',message='分页读取已停止');return
        items=page.get('items',[])
        while len(json.dumps(page,ensure_ascii=False,default=wire_default).encode())>512*1024 and len(items)>1:
            items.pop();page['next_offset']=args.get('offset',0)+len(items)
        if len(json.dumps(page,ensure_ascii=False,default=wire_default).encode())>1024*1024:raise ValueError('单条证据过长，请在原生商品详情查看')
        report['pages'][key]=page
        while len(report['pages'])>8:report['pages'].pop(next(iter(report['pages'])))
        gateway.journal.update(task['task_id'],state='completed',message='分页完成；使用原分析task_id和同一组参数再次读取')
    gateway.start_job(task,work,finish)
    return {'page_pending':True,'page_task_id':task['task_id'],'analysis_task_id':args['task_id'],
            'next':'等待page_task_id完成后，用原analysis_task_id及相同参数再次调用当前工具'}


def export(gateway,args):
    import stockout_ai as service
    import export_settings
    key='stockout-export:'+args['operation_key'];old=gateway.journal.lookup(key,args)
    if old:return gateway.task_record(old['task_id'])
    gateway.idle();gateway.check_context(args['context_token']);report=require(gateway,args['task_id'])
    if report['snapshot_id']!=args['snapshot_id']:raise CommandError('STALE_CONTEXT','分析快照不一致，请先读取原分析结果')
    directory=args.get('directory') or export_settings.load(Path(folder(gateway))/export_settings.FILENAME)
    if not directory:raise CommandError('NEEDS_REVIEW','请设置导出位置或指定本地目录')
    target=Path(export_settings.validate(directory,True))/('门店缺货率分析_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.xlsx')
    task,_=gateway.journal.create(key,args,'stockout_export')
    def finish(receipt):gateway.journal.update(task['task_id'],state='completed',message='同一日报快照已导出，原业务资料未变',receipt=receipt)
    gateway.start_job(task,lambda w:service.export(folder(gateway),report,target,args.get('combinations'),args.get('include_details',False),w.progress.emit,w.cancel),finish)
    return task
