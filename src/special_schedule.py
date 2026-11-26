"""Durable, explicitly approved local allocation tickets for manual, bulk and timed entry points."""
import json
import sqlite3
from PySide6.QtCore import QTimer

import special_plans as sp
import special_waves as waves
import work_repository as wr

def register(path,preview):
    view=preview['view']
    with sp.connection(path,True) as db:
        plan=sp.read(db,view['plan'])
        if not sp.enabled(db,plan['kind']) or plan['version']!=view['version']:
            raise ValueError('请核对专项开关和已发布版本')
        row=db.execute('SELECT sha,state FROM special_tickets WHERE plan=? AND wave=?',(view['plan'],view['wave'])).fetchone()
        if row and row[0]!=preview['sha'] and row[1]!='待复核':
            raise wr.Conflict('同轮已有不同的原定时任务')
        db.execute("""INSERT INTO special_tickets VALUES(?,?,?,?,'等待','') ON CONFLICT(plan,wave)
                   DO UPDATE SET sha=excluded.sha,body=excluded.body,state='等待',message='' WHERE special_tickets.state='待复核'""",
                   (view['plan'],view['wave'],preview['sha'],wr.dumps(preview)))

def dispatch(path):
    with sp.connection(path) as db:
        if not sp.exists(db,'special_tickets'):
            return []
        tickets=list(db.execute("SELECT plan,wave,body FROM special_tickets WHERE state='等待' ORDER BY rowid"))
    done=[]
    for plan,wave,raw in tickets:
        preview=json.loads(raw)
        with sp.connection(path) as db:
            obj=sp.read(db,plan)
            if not sp.enabled(db,obj['kind']):
                continue
        at=sp.instant(sp.now())
        lines=[r for r in preview['view']['rows'] if sp.number(r['quantity'],'数量')]
        if any(at<sp.instant(r.get('start',obj['start'])) for r in lines) or at<sp.instant(obj['start']):
            continue
        try:
            result=waves.claim(path,preview)
            state,message='已处理','原轮次含 '+str(len(result))+' 张执行仓单'
        except (ValueError,wr.Conflict) as exc:
            state,message='待复核',str(exc)
        with sp.connection(path,True) as db:
            db.execute("UPDATE special_tickets SET state=?,message=? WHERE plan=? AND wave=? AND sha=? AND state='等待'",
                       (state,message,plan,wave,preview['sha']))
        done.append((plan,wave,state,message))
    return done

def install(host):
    from concurrent.futures import ThreadPoolExecutor
    timer=QTimer(host)
    pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='special-wave')
    path=host.work_repository.path
    future=None
    def tick():
        nonlocal future
        if not getattr(host,'closing',False) and (future is None or future.done()):
            future=pool.submit(dispatch,path)
    timer.timeout.connect(tick)
    timer.start(15000)
    host.destroyed.connect(lambda *_:pool.shutdown(wait=False,cancel_futures=True))
    host.special_scheduler=timer
