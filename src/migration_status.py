"""A sibling handoff record survives UI destruction and directory replacement."""
import time,traceback
from pathlib import Path
from data_location_status import read, save
from stockout_common import now

def folder(root):return Path(root).resolve().parent/('.'+Path(root).name+'.order-migrations')
def record(plan,state,*,phase=None,error=None,result=None,progress=None):
    from order_migration import checked_plan
    root=checked_plan(plan);dest=folder(root);dest.mkdir(parents=True,exist_ok=True);name=plan['nonce']
    original=dest/(name+'.plan.json')
    if not original.exists():save(original,plan)
    file=dest/(name+'.status.json');previous=read(file) if file.is_file() else {}
    value={'nonce':name,'state':state,'phase':phase or state,'at':now(),'root':str(root),'stage':plan['stage'],'backup':plan['backup'],'plan_file':str(original)}
    if error is not None:value['error']={'type':type(error).__name__,'message':str(error),'traceback':''.join(traceback.format_exception(type(error),error,error.__traceback__))}
    if result is not None:value['result']=result
    if progress is not None:value['progress']=progress
    history=previous.get('history',[])
    if (previous.get('state'),previous.get('phase'))!=(state,value['phase']):
        history=[*history,{k:value[k] for k in ('state','phase','at')}][-80:]
    value['history']=history
    save(file,value)
    pointer=dest/'latest.json'
    try:latest_nonce=read(pointer).get('nonce')
    except (OSError,ValueError,AttributeError):latest_nonce=None
    if latest_nonce!=name:save(pointer,{'nonce':name})
    return value


def reporter(plan,phase):
    """Persist running activity at most once a second; transitions bypass this."""
    from export_progress import RowProgress
    last=None
    def report(message):
        nonlocal last
        at=time.monotonic()
        if last is not None and at-last<1:return
        detail={'label':str(message)}
        if isinstance(message,RowProgress):detail.update(current=message.current,total=message.total)
        record(plan,'APPLYING',phase=phase,progress=detail);last=at
    return report
def latest(root):
    dest=folder(root);pointer=dest/'latest.json'
    if not pointer.is_file():return None
    import re
    name=read(pointer).get('nonce','')
    if not re.fullmatch('[0-9a-f]{32}',name):raise ValueError('迁入状态身份无效，请保留诊断记录')
    value=read(dest/(name+'.status.json'))
    if Path(value['root']).resolve()!=Path(root).resolve():raise ValueError('迁入状态目录不一致')
    return value
def waiting(host,plan):
    value=record(plan,'WAITING_EXIT');host._order_import_request=plan
    from application_runtime import current
    r=current()
    if r:r.request=plan;r.shutdown_errors=0;r.first_shutdown_exception=None;r.transition('WAITING_EXIT')
    return value
def shutdown_failed(host,error):
    plan=getattr(host,'_order_import_request',None)
    location_plan=getattr(host,'_data_location_request',None)
    if plan is None and location_plan is None:return False
    if plan:record(plan,'FAILED',phase='WAITING_EXIT',error=error)
    if location_plan:
        import data_relocation
        data_relocation.record(location_plan,'FAILED','WAITING_EXIT','后台未能安全退出，资料位置未切换',error={'message':str(error),'traceback':''.join(traceback.format_exception(type(error),error,error.__traceback__))})
        host._data_location_request=None
    host._order_import_request=None;host._discard_close=False;host.closing=False;host.setEnabled(True)
    pause=getattr(host,'_data_location_pause',None)
    manager=getattr(host,'aoxiang_workflows',None)
    if manager:manager.resume_after_cancelled_close()
    for name in ('browser_startup','browser_status'):
        owner=getattr(host,name,None)
        if owner:
            if hasattr(owner,'closing'):owner.closing=False
            if hasattr(owner,'close_requested'):owner.close_requested=False
    # Generic cancelled-close recovery may enable a queue. Restore the exact
    # maintenance-entry state afterwards, including queues already stopped.
    if pause:pause.restore()
    host._aoxiang_close_requested=False
    from application_runtime import current
    r=current()
    if r:r.exception(type(error),error,error.__traceback__);r.request=None;r.storage_request=None;r.transition('RUNNING')
    host.status.setText('资料搬迁未执行：后台未能安全退出，原位置保持。请处理原连接后重试。' if location_plan else '迁入未执行：后台未能安全退出。原库和迁入材料已保留；请查看迁入结果，修复连接后重新校验原包。')
    return True
