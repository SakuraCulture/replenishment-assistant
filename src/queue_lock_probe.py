"""Opt-in isolated EXE acceptance for live Qt polling and SQLite contention."""
import json,os,sqlite3,time
from pathlib import Path
from PySide6.QtCore import QTimer,QEventLoop

def exercise(app,manager,out):
    out=Path(out).resolve()
    if Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings' or manager.store.folder.resolve()!=out/'settings/aoxiang':raise ValueError('Only isolated QA settings permitted')
    def check(ok,text):
        if not ok:raise AssertionError(text)
    def wait(ms):
        loop=QEventLoop();QTimer.singleShot(ms,loop.quit);loop.exec()
    old_interval=manager.timer.interval();manager.timer.setInterval(30)
    ticks=[];beat=QTimer();beat.setInterval(10);beat.timeout.connect(lambda:ticks.append(time.perf_counter()));beat.start()
    results={};block=None
    try:
        # There must be no browser/real business work in this isolated probe.
        check(not manager.store.list() and not manager.store.schedules(),'QA queue must be empty')
        with manager.store.db(True) as db:
            s={'id':'qa-lock-only','enabled':True,'terminal':False,'next_at':0,'basis':{},'order_id':'QA'}
            db.execute('INSERT INTO ax_schedules VALUES(?,?)',(s['id'],json.dumps(s)))
        block=sqlite3.connect(manager.store.path);block.execute('BEGIN IMMEDIATE')
        started=time.perf_counter();manager.tick()
        results['blocked_poll_seconds']=time.perf_counter()-started
        check(results['blocked_poll_seconds']<.5,'Poll waited on SQLite lock in UI thread')
        start=time.perf_counter();wait(260)
        results.update(busy_observed=manager.busy,heartbeats_while_busy=len(ticks),busy_wait_seconds=time.perf_counter()-start,
                       owner_retained=manager.owner is not None,stopped=manager.stopping)
        check(manager.busy and not manager.stopping and len(ticks)>=5,'Lock wait blocked UI or permanently stopped queue')
        # Disable the synthetic schedule before releasing the lock: never dispatch it.
        block.execute('DELETE FROM ax_schedules WHERE id=?',('qa-lock-only',));block.commit();block.close();block=None
        wait(150);check(not manager.busy and not manager.stopping,'Did not recover after release')
        manager.ensure_owner();results['automatic_recovery']=True
        for mode in ('read','write'):
            before=len(ticks)
            with manager.maintenance():
                block=sqlite3.connect(manager.store.path)
                block.execute('BEGIN' if mode=='read' else 'BEGIN EXCLUSIVE')
                block.execute('SELECT * FROM ax_schedules').fetchall()
                wait(180)
                check(not manager.busy and manager.maintenance_depth==1,'Maintenance did not suspend polling')
                block.rollback();block.close();block=None
            wait(70);check(not manager.stopping and manager.maintenance_depth==0,'Maintenance did not release gate')
            results[mode+'_maintenance_heartbeats']=len(ticks)-before
        check(not manager.store.list() and not manager.store.schedules() and manager.worker is None,'Unexpected dispatch')
        results['no_platform_task_dispatched']=True;results['passed']=True
        return ['live Qt timer: lock busy keeps UI responsive; unlock recovers; read/write maintenance gates release; no platform dispatch']
    finally:
        if block:block.rollback();block.close()
        beat.stop();manager.timer.setInterval(old_interval)
        (out/'queue-lock-report.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
