"""Bounded schedule marker; importing/reading never opens an analysis database."""
from datetime import datetime,timedelta
from pathlib import Path
import re
from stockout_common import TZ,atomic_json,read_json


def path(folder,filename='stockout-schedule.json'):return Path(folder)/'analysis'/filename


def load(folder,*,filename='stockout-schedule.json'):
    source=path(folder,filename)
    if not source.exists():return {'enabled':False}
    value=read_json(source,8192)
    if not isinstance(value,dict) or type(value.get('enabled')) is not bool:
        raise ValueError('缺货分析采集计划标记无效，未启动后台工作')
    if value['enabled']:
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',value.get('at','')):
            raise ValueError('分析计划时间无效')
        next_at=datetime.fromisoformat(value['next_at'])
        if next_at.tzinfo is None:raise ValueError('分析计划缺少北京时间依据')
    return value


def next_time(at,clock=None):
    clock=(clock or datetime.now(TZ)).astimezone(TZ)
    hour,minute=map(int,at.split(':'))
    result=clock.replace(hour=hour,minute=minute,second=0,microsecond=0)
    if result<=clock:result+=timedelta(days=1)
    return result.isoformat()


def save(folder,enabled,at='08:00',clock=None,*,filename='stockout-schedule.json'):
    if type(enabled) is not bool or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',at):
        raise ValueError('请输入有效的每日采集时间')
    value={'schema':1,'enabled':enabled,'at':at,'next_at':next_time(at,clock) if enabled else None}
    atomic_json(path(folder,filename),value);return value


def due(folder,clock=None,*,filename='stockout-schedule.json'):
    value=load(folder,filename=filename);clock=(clock or datetime.now(TZ)).astimezone(TZ)
    if not value['enabled']:return None
    when=datetime.fromisoformat(value['next_at']).astimezone(TZ)
    if clock<when:return None
    value['next_at']=next_time(value['at'],clock)
    value['last_due_day']=when.date().isoformat()
    atomic_json(path(folder,filename),value)
    if when.date()!=clock.date():
        return {'missed':True,'message':'分析计划日期已错过，未拿今天库存补历史；可进入板块手动采集今日'}
    return {'trigger':'schedule:'+clock.date().isoformat()}
