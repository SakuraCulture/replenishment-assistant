"""Durable allocation outbox written inside the lifecycle transaction."""
import sqlite3
from contextlib import closing

import warehouse_execution
import work_repository as wr
from deletion_barrier import require
from jst_order_store import digest
from lifecycle_routes import demand_stage
from lifecycle_store import saved_at
from runtime_support import checkpoint
from order_keys import warehouse_revision


def schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS jst_tasks(task TEXT PRIMARY KEY,created TEXT,head TEXT,token TEXT,status TEXT,plan TEXT,message TEXT)')


def enqueue(db, head, token):
    require(db,head['oid'])
    warehouse_execution.validate(db,head)
    schema(db)
    h={k:v for k,v in head.items() if k!='products'}
    identity=digest([warehouse_revision(h),token])
    row=saved_at(db, h, demand_stage(db,h), token['version'])
    db.execute('INSERT OR IGNORE INTO jst_tasks VALUES(?,?,?,?,?,?,?)',
               (identity,row[0] if row else wr.now(),wr.dumps(h),wr.dumps(token),'pending',None,'回传已确认，待准备分配批次'))


def pending(path):
    with closing(sqlite3.connect(path)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='jst_tasks'").fetchone():return []
        return db.execute("SELECT task,head,token,plan FROM jst_tasks WHERE status IN ('pending','prepared') ORDER BY created,json_extract(head,'$.day'),json_extract(head,'$.number'),rowid LIMIT 100").fetchall()


def latest_statuses(db, batches, cancel=None):
    """Read the last task presentation state for each selected warehouse order."""
    tasks = {}
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='jst_tasks'").fetchone():
        for placeholders,oids in batches or [('',[])]:
            checkpoint(cancel)
            # A malformed identity cannot be assigned outside the scope;
            # retain the previous decode/error behavior instead of hiding it.
            identity="json_type(head,'$.oid') IS NOT 'text' OR json_type(head,'$.warehouse') IS NOT 'text'"
            if oids:identity+=' OR json_extract(head,\'$.oid\') IN ('+placeholders+')'
            condition='CASE WHEN json_valid(head) THEN ('+identity+') ELSE 1 END'
            for raw,status,message in db.execute('SELECT head,status,message FROM jst_tasks WHERE '+condition+' ORDER BY created,rowid',oids):
                h=wr.loads(raw)
                tasks[(h['oid'],h['warehouse'])]={'status':status,'message':message}
    return tasks
