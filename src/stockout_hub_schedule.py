"""Small schedule markers only. Heavy analysis imports stay in the due worker."""
from datetime import datetime
from pathlib import Path
from PySide6.QtCore import QTimer
import stockout_trigger as trigger
from stockout_common import TZ,read_json,atomic_json,WAREHOUSES
from stockout_schedule_queue import Queue,SOURCES as FILES


def plans(folder):
    result=[];values=Queue(folder).plans()
    for filename,mode in FILES:
        row=values[filename]
        if row['enabled']:
            mode=mode or row.get('mode')
            if mode not in ('store','warehouse','both'):raise ValueError('统一分析计划范围无效')
            result.append({**row,'filename':filename,'mode':mode})
    return result


def arm(entry):
    if entry.timer:entry.timer.stop();entry.timer.deleteLater();entry.timer=None
    if entry.closing or not owns_dispatch(entry):return
    try:
        folder=entry.host.repository.folder;rows=plans(folder)
        if Queue(folder).pending():entry.ensure_watch()
        if not rows:return
        when=min(datetime.fromisoformat(r['next_at']) for r in rows)
        delay=max(1000,int((when-datetime.now(TZ)).total_seconds()*1000))
        entry.timer=QTimer(entry);entry.timer.setSingleShot(True);entry.timer.timeout.connect(entry.due);entry.timer.start(min(delay,2147483647))
    except Exception as exc:entry.status('缺货率计划未启动：'+str(exc))


def owns_dispatch(entry):
    manager=getattr(entry.host,'scheduled',None)
    return manager is None or manager.owner


def due(entry,clock=None):
    if not owns_dispatch(entry):entry.arm();return
    folder=entry.host.repository.folder
    clock=clock or datetime.now(TZ)
    from stockout_hub_service import preview
    selected=Path(folder)/'analysis'/'warehouse_stockout'/'selection.json'
    default_warehouses=read_json(selected,8192) if selected.exists() else list(WAREHOUSES)
    def freeze(when,parts):
        modes,warehouses=set(),set()
        for _,mode,row in parts:
            modes.update(('store','warehouse') if mode=='both' else (mode,))
            if mode!='store':warehouses.update(row.get('warehouses',default_warehouses))
        mode='both' if len(modes)==2 else next(iter(modes))
        when=when.isoformat()
        request={'mode':mode,'warehouses':sorted(warehouses),'trigger':'schedule:'+when[:10]+':'+when[11:16]+':'+mode}
        try:request['plan']=preview(folder,mode,request['warehouses'])
        except (ValueError,OSError) as exc:request['input_error']=str(exc)
        return request
    try:
        rows=Queue(folder).due(clock,freeze)
        if any(row['state']=='MISSED' for row in rows):entry.status('原日期已错过，已保留错过记录')
        drain(entry)
    except Exception as exc:entry.status('缺货采集登记待重试：'+str(exc))
    finally:entry.arm()


def drain(entry):
    if not owns_dispatch(entry) or entry.closing:return
    queue=Queue(entry.host.repository.folder)
    if not queue.path.is_file():return
    if not getattr(entry,'scheduled_recovered',False):
        queue.recover();entry.scheduled_recovered=True
    if not queue.pending():return
    if not entry.worker and not entry.queued and not entry.busy():
        row=queue.claim()
        if row:
            from stockout_common import today
            args=row['request']
            if row['day']!=today() or args.get('input_error'):
                message=args.get('input_error') or '原已登记采集已跨日，继续前请核对原任务'
                queue.finish(row['id'],row['epoch'],'REVIEW',message);entry.status(message)
            else:
                entry.scheduled_claim=row
                entry.start('hub_scheduled',{**args,'scheduled_id':row['id'],'scheduled_epoch':row['epoch']},background=True)
    if queue.pending():entry.ensure_watch()


def save(folder,enabled,at,mode,warehouses=None):
    if mode not in ('store','warehouse','both'):raise ValueError('请选择计划范围')
    if warehouses is not None and mode!='store' and (not isinstance(warehouses,list) or not warehouses or len(warehouses)!=len(set(warehouses)) or set(warehouses)-set(WAREHOUSES)):raise ValueError('请选择计划输出仓')
    # The dialog explicitly replaces the displayed old schedules, keeping their
    # files and disabled state rather than silently enabling an additional job.
    import re
    if type(enabled) is not bool or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',at):raise ValueError('请输入有效的每日采集时间')
    value={'schema':1,'enabled':enabled,'at':at,'next_at':trigger.next_time(at) if enabled else None,'mode':mode}
    if warehouses is not None:value['warehouses']=warehouses if mode!='store' else []
    queue=Queue(folder);queue.save(FILES[-1][0],value,disable_legacy=True)
    atomic_json(trigger.path(folder,'stockout-hub-schedule.json'),value)
    for filename,old in queue.plans().items():
        if filename!=FILES[-1][0] and trigger.path(folder,filename).exists():atomic_json(trigger.path(folder,filename),old)
    return value
