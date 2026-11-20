"""Persistent allocation batch headers, membership and stored context.

These queries can run on the operation's transaction or on a read-only
connection. They never create or update a batch.
"""
from contextlib import closing

from jst_order_store import decode
from runtime_support import checkpoint
import work_repository as wr


ZERO_OUTCOME = '无可分配库存'


def exists(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='jst_batches'").fetchone())


def state(db, batch_id, warehouse):
    return db.execute('SELECT bid,serial FROM jst_batch_stock WHERE batch_id=? AND warehouse=?', (batch_id, warehouse)).fetchone() if exists(db) else None


def generation_batch(db, gid):
    if not exists(db): return None
    r = db.execute('SELECT batch_id,position FROM jst_generation_batches WHERE gid=?', (gid,)).fetchone()
    return r[0] if r else None


def context(db, batch_id, *, load_stock=True):
    if not exists(db): raise ValueError('分配批次不存在')
    row = db.execute('SELECT payload,digest,status,epoch,label FROM jst_batches WHERE batch_id=?', (batch_id,)).fetchone()
    if not row: raise ValueError('分配批次不存在')
    spec = decode(row[:2]); spec.update(status=row[2], epoch=row[3], label=row[4], stocks={}, states={})
    for wh, bid, serial in db.execute('SELECT warehouse,bid,serial FROM jst_batch_stock WHERE batch_id=? ORDER BY rowid', (batch_id,)):
        if load_stock: spec['stocks'][wh] = decode(db.execute('SELECT payload,digest FROM jst_baselines WHERE bid=?', (bid,)).fetchone())
        spec['states'][wh] = (bid, serial)
    spec['members'] = [dict(position=p, head=wr.loads(h), status=s, message=m) for p,h,s,m in
                       db.execute('SELECT position,head,status,message FROM jst_batch_members WHERE batch_id=? ORDER BY position', (batch_id,))]
    return spec


def listing(path, progress=lambda _: None, cancel=None):
    with closing(wr.read_connection(path)) as db:
        if not exists(db): return []
        out = []
        for bid,created,label,status in db.execute('SELECT batch_id,created,label,status FROM jst_batches ORDER BY rowid DESC'):
            checkpoint(cancel)
            counts = dict(db.execute('SELECT status,count(*) FROM jst_batch_members WHERE batch_id=? GROUP BY status', (bid,)))
            out.append(dict(batch_id=bid, created=created, label=label, status=status, counts=counts))
        return out
