"""Small schedule markers only. Heavy analysis imports stay in the due worker."""
from datetime import datetime
from pathlib import Path
from PySide6.QtCore import QTimer
import stockout_trigger as trigger
from stockout_common import TZ,read_json,atomic_json,WAREHOUSES

FILES=(('stockout-schedule.json','store'),('warehouse-stockout-schedule.json','warehouse'),('stockout-hub-schedule.json',None))


def plans(folder):
    result=[]
    for filename,mode in FILES:
        row=trigger.load(folder,filename=filename)
        if row['enabled']:
            mode=mode or row.get('mode')
            if mode not in ('store','warehouse','both'):raise ValueError('统一分析计划范围无效')
            result.append({**row,'filename':filename,'mode':mode})
    return result


def arm(entry):
    if entry.timer:entry.timer.stop();entry.timer.deleteLater();entry.timer=None
    if entry.closing:return
    try:
        rows=plans(entry.host.repository.folder)
        if not rows:return
        when=min(datetime.fromisoformat(r['next_at']) for r in rows)
        delay=max(1,int((when-datetime.now(TZ)).total_seconds()*1000))
        entry.timer=QTimer(entry);entry.timer.setSingleShot(True);entry.timer.timeout.connect(entry.due);entry.timer.start(min(delay,2147483647))
    except Exception as exc:entry.status('缺货率计划未启动：'+str(exc))


def due(entry):
    folder=entry.host.repository.folder;groups={}
    selected=Path(folder)/'analysis'/'warehouse_stockout'/'selection.json'
    default_warehouses=read_json(selected,8192) if selected.exists() else list(WAREHOUSES)
    for row in plans(folder):
        outcome=trigger.due(folder,filename=row['filename'])
        if not outcome:continue
        if outcome.get('missed'):entry.status(outcome['message']);continue
        group=groups.setdefault(row['next_at'],{'modes':set(),'warehouses':set()})
        group['modes'].update(('store','warehouse') if row['mode']=='both' else (row['mode'],))
        if row['mode']!='store':group['warehouses'].update(row.get('warehouses',default_warehouses))
    queue=getattr(entry,'scheduled_queue',[])
    for when,group in groups.items():
        modes=group['modes']
        mode='both' if len(modes)==2 else next(iter(modes))
        queue.append({'mode':mode,'warehouses':sorted(group['warehouses']),'trigger':'schedule:'+when[:10]+':'+when[11:16]+':'+mode})
    entry.scheduled_queue=queue
    drain(entry);entry.arm()


def drain(entry):
    queue=getattr(entry,'scheduled_queue',[])
    if queue and not entry.worker and not entry.queued and not entry.busy() and not entry.closing:
        args=queue.pop(0)
        from stockout_common import today
        if args['trigger'].split(':',1)[1][:10]!=today():entry.status('排队计划已跨日，未补写旧日库存')
        else:entry.start('hub_scheduled',args,background=True)
    if queue:entry.ensure_watch()


def save(folder,enabled,at,mode,warehouses=None):
    if mode not in ('store','warehouse','both'):raise ValueError('请选择计划范围')
    if warehouses is not None and mode!='store' and (not isinstance(warehouses,list) or not warehouses or len(warehouses)!=len(set(warehouses)) or set(warehouses)-set(WAREHOUSES)):raise ValueError('请选择计划输出仓')
    # The dialog explicitly replaces the displayed old schedules, keeping their
    # files and disabled state rather than silently enabling an additional job.
    value=trigger.save(folder,enabled,at,filename='stockout-hub-schedule.json');value['mode']=mode
    if warehouses is not None:value['warehouses']=warehouses if mode!='store' else []
    atomic_json(trigger.path(folder,'stockout-hub-schedule.json'),value)
    for filename,_ in FILES[:2]:
        old=trigger.load(folder,filename=filename)
        if old['enabled']:trigger.save(folder,False,old.get('at','08:00'),filename=filename)
    return value
