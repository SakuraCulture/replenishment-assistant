"""Durable return-triggered generation. Payment facts never reserve stock.

Prepared operations are persisted before execution and reused after interruption.
SQLite CAS plus the generation service protect competing consumers.
"""
import sqlite3
from contextlib import closing
from sqlite_write import begin
from jst_task_store import enqueue, pending, schema
from runtime_support import checkpoint


def mark(path,task,status,message):
    with closing(sqlite3.connect(path,timeout=.1)) as db,db:
        begin(db,None)
        db.execute('UPDATE jst_tasks SET status=?,message=? WHERE task=?',(status,message,task))


def retry(path, heads, progress=lambda _:None,cancel=None):
    checkpoint(cancel)
    raise ValueError('旧版勾选重试入口已停用，未执行任何订单。请打开“聚水潭导入单”选择原分配批次，核对范围和原顺序后继续。')


def drain(path, folder, progress=lambda _:None,cancel=None,*,before_work=lambda:None):
    # Unassigned/legacy outbox entries remain visible, but never silently form
    # one batch per store or adopt another batch's current stock.
    import jst_batches
    return jst_batches.drain(path,progress,cancel,before_work=before_work)
