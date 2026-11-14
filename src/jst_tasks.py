"""Durable return-triggered generation. Payment facts never reserve stock.

Prepared operations are persisted before execution and reused after interruption.
SQLite CAS plus the generation service protect competing consumers.
"""
import sqlite3
from contextlib import closing
import order_lifecycle as lc
import jst_orders as j
import work_repository as wr
from runtime_support import checkpoint, Cancelled
from export_progress import RowProgress


def schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS jst_tasks(task TEXT PRIMARY KEY,created TEXT,head TEXT,token TEXT,status TEXT,plan TEXT,message TEXT)')


def enqueue(db, head, token):
    from deletion_barrier import require
    require(db,head['oid'])
    import warehouse_execution
    warehouse_execution.validate(db,head)
    schema(db)
    h={k:v for k,v in head.items() if k!='products'}
    identity=j.digest([lc.key(h),token])
    row=db.execute('SELECT saved FROM lifecycle_versions WHERE oid=? AND revision=? AND warehouse=? AND stage=? AND version=?',(*lc.key(h),lc.flow.demand_stage(db,h),token['version'])).fetchone()
    db.execute('INSERT OR IGNORE INTO jst_tasks VALUES(?,?,?,?,?,?,?)',
               (identity,row[0] if row else wr.now(),wr.dumps(h),wr.dumps(token),'pending',None,'回传已确认，待准备分配批次'))


def pending(path):
    with closing(sqlite3.connect(path)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='jst_tasks'").fetchone():return []
        return db.execute("SELECT task,head,token,plan FROM jst_tasks WHERE status IN ('pending','prepared') ORDER BY created,json_extract(head,'$.day'),json_extract(head,'$.number'),rowid LIMIT 100").fetchall()


def mark(path,task,status,message):
    with closing(sqlite3.connect(path,timeout=.1)) as db,db:
        lc.begin(db,None)
        db.execute('UPDATE jst_tasks SET status=?,message=? WHERE task=?',(status,message,task))


def retry(path, heads, progress=lambda _:None,cancel=None):
    import jst_batches as batches
    checkpoint(cancel);ids=set()
    with closing(sqlite3.connect(path)) as db:
        if batches.exists(db):
            for h in heads:
                ids.update(r[0] for r in db.execute("SELECT batch_id FROM jst_batch_members WHERE oid=? AND warehouse=? AND status IN ('pending','failed')",(h['oid'],h['warehouse'])))
    if len(ids)!=1:raise ValueError('请选择同一未完成分配批次；旧任务或尚未分批的回传请先准备新批次')
    plan=batches.preview_existing(path,next(iter(ids)),progress=progress,cancel=cancel)
    batches.activate(path,plan,progress,cancel)


def drain(path, folder, progress=lambda _:None,cancel=None,*,before_work=lambda:None):
    # Unassigned/legacy outbox entries remain visible, but never silently form
    # one batch per store or adopt another batch's current stock.
    import jst_batches
    return jst_batches.drain(path,progress,cancel,before_work=before_work)
