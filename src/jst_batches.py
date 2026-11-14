"""Explicit allocation batches. Frozen stock, scoped reservations, resumable order.

The numeric allocator remains jst_orders/jst_allocation. Legacy generations are
read as recorded; neither dates nor file hashes are used to infer old batches.
"""
import copy
import hashlib
import sqlite3
import uuid
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import jst_orders as orders
import order_lifecycle as lc
import work_repository as wr
import warehouse_catalog as wc
import storage_chunks
from runtime_support import checkpoint, Cancelled, CommitProgress


def exists(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='jst_batches'").fetchone())


def schema(db):
    orders.schema(db)
    for sql in (
        'CREATE TABLE IF NOT EXISTS jst_batches(batch_id TEXT PRIMARY KEY,created TEXT,label TEXT,status TEXT,epoch INTEGER,payload TEXT,digest TEXT)',
        'CREATE TABLE IF NOT EXISTS jst_batch_stock(batch_id TEXT,warehouse TEXT,bid TEXT,serial INTEGER,PRIMARY KEY(batch_id,warehouse))',
        'CREATE TABLE IF NOT EXISTS jst_batch_members(batch_id TEXT,position INTEGER,oid TEXT,warehouse TEXT,head TEXT,status TEXT,message TEXT,PRIMARY KEY(batch_id,position),UNIQUE(batch_id,oid,warehouse))',
        'CREATE TABLE IF NOT EXISTS jst_generation_batches(gid TEXT PRIMARY KEY,batch_id TEXT,position INTEGER)',
        'CREATE INDEX IF NOT EXISTS jst_batch_member_order ON jst_batch_members(oid,warehouse)',
    ): db.execute(sql)


def state(db, batch_id, warehouse):
    return db.execute('SELECT bid,serial FROM jst_batch_stock WHERE batch_id=? AND warehouse=?', (batch_id, warehouse)).fetchone() if exists(db) else None


def generation_batch(db, gid):
    if not exists(db): return None
    r = db.execute('SELECT batch_id,position FROM jst_generation_batches WHERE gid=?', (gid,)).fetchone()
    return r[0] if r else None


def reservations(db, batch_id, warehouse, exclude=()):
    values = {}; exclude = set(exclude)
    if not exists(db): return values
    rows = db.execute('''SELECT a.gid,r.sku,r.quantity FROM jst_active a
        JOIN jst_generation_batches b ON b.gid=a.gid JOIN jst_reservations r ON r.gid=a.gid
        WHERE b.batch_id=? AND a.warehouse=? ORDER BY b.position,r.rowid''', (batch_id, warehouse))
    for gid, sku, qty in rows:
        if gid not in exclude: values[sku] = values.get(sku, Decimal(0)) + Decimal(qty)
    # Thoroughly deleted locked orders must not return stock to this same batch.
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='jst_batch_retired'").fetchone():
        for sku, qty in db.execute('SELECT sku,quantity FROM jst_batch_retired WHERE batch_id=? AND warehouse=?', (batch_id, warehouse)):
            values[sku] = values.get(sku, Decimal(0)) + Decimal(qty)
    return values


def context(db, batch_id, *, load_stock=True):
    if not exists(db): raise ValueError('分配批次不存在')
    row = db.execute('SELECT payload,digest,status,epoch,label FROM jst_batches WHERE batch_id=?', (batch_id,)).fetchone()
    if not row: raise ValueError('分配批次不存在')
    spec = orders.decode(row[:2]); spec.update(status=row[2], epoch=row[3], label=row[4], stocks={}, states={})
    for wh, bid, serial in db.execute('SELECT warehouse,bid,serial FROM jst_batch_stock WHERE batch_id=? ORDER BY rowid', (batch_id,)):
        if load_stock: spec['stocks'][wh] = orders.decode(db.execute('SELECT payload,digest FROM jst_baselines WHERE bid=?', (bid,)).fetchone())
        spec['states'][wh] = (bid, serial)
    spec['members'] = [dict(position=p, head=wr.loads(h), status=s, message=m) for p,h,s,m in
                       db.execute('SELECT position,head,status,message FROM jst_batch_members WHERE batch_id=? ORDER BY position', (batch_id,))]
    return spec


def listing(path, progress=lambda _: None, cancel=None):
    with closing(sqlite3.connect(path)) as db:
        if not exists(db): return []
        out = []
        for bid,created,label,status in db.execute('SELECT batch_id,created,label,status FROM jst_batches ORDER BY rowid DESC'):
            checkpoint(cancel)
            counts = dict(db.execute('SELECT status,count(*) FROM jst_batch_members WHERE batch_id=? GROUP BY status', (bid,)))
            out.append(dict(batch_id=bid, created=created, label=label, status=status, counts=counts))
        return out


def preview_new(path, heads, catalogs, manual_order=False, progress=lambda _: None, cancel=None):
    """No writes/reservations; selecting the same file explicitly is allowed."""
    checkpoint(cancel); stocks = {}; states = {}; batch_id = uuid.uuid4().hex
    with closing(sqlite3.connect(path)) as db:
        for h in heads:
            if exists(db) and db.execute("SELECT 1 FROM jst_batch_members WHERE oid=? AND warehouse=? AND status IN ('pending','failed')", (h['oid'], h['warehouse'])).fetchone():
                raise wr.Conflict('所选订单已有未完成批次，请继续原批或明确撤销：' + h['number'])
    for wh in dict.fromkeys(h['warehouse'] for h in heads):
        if wh not in catalogs: raise ValueError(wh + '尚未选用商品资料')
        checkpoint(cancel); c = wc.validate(copy.deepcopy(catalogs[wh])); bid = uuid.uuid4().hex
        if c['warehouse'] != wh: raise ValueError('选用资料与供货仓不一致')
        stocks[wh] = dict(bid=bid, warehouse=wh, catalog=c, included=[], created=wr.now(), batch_id=batch_id)
        states[wh] = (bid, 0)
    spec = dict(batch_id=batch_id, created=wr.now(), epoch=0, stocks=stocks, states=states, members=[], new=True)
    plan = orders.prepare_checked(path, heads, manual_order, progress=progress, cancel=cancel, batch=spec)
    spec['members'] = [dict(position=p['position'], head=p['head']) for p in plan['payload']['plans']]
    if not spec['members']: raise ValueError('没有可加入新批次的订单；已生成单请导出或进入原批次')
    spec['label'] = wr.now() + f" · {len(spec['members'])}张仓单"
    plan['payload']['new_batch'] = spec; plan['digest'] = orders.digest(plan['payload'])
    return plan


def preview_existing(path, batch_id, heads=None, correction=False, progress=lambda _: None, cancel=None):
    with closing(sqlite3.connect(path)) as db: spec = context(db, batch_id)
    allowed = {(m['head']['oid'], m['head']['warehouse']): m for m in spec['members']}
    chosen = {(h['oid'], h['warehouse']) for h in heads} if heads is not None else None
    if chosen is not None and not chosen <= allowed.keys(): raise ValueError('不能把其他批次订单加入本批')
    selected = [m['head'] for m in spec['members'] if (chosen is None or (m['head']['oid'],m['head']['warehouse']) in chosen)
                and (m['status']=='done' if correction else m['status'] in ('pending','failed'))]
    if not selected: raise ValueError('本批没有可继续的任务；已生成单可直接导出')
    plan=orders.prepare_checked(path, selected, True, correction, progress, cancel, batch=spec)
    if not plan['payload']['plans']:
        reasons='；'.join(p['head']['number']+'：'+p['reason'] for p in plan['payload']['skipped'])
        raise ValueError('所选订单暂不能重新分配：'+reasons if correction else '本批没有可继续生成的订单：'+reasons)
    return plan


def _pause_other(db, batch_id):
    db.execute("UPDATE jst_batches SET status='paused',epoch=epoch+1 WHERE batch_id<>? AND status='active'", (batch_id,))


def register(path, plan, progress=lambda _:None, cancel=None):
    """Persist exact preview and membership atomically before any allocation."""
    v = plan['payload']; spec = v.get('new_batch')
    if orders.digest(v) != plan['digest']: raise ValueError('分配预览已变化，请重新预览')
    if not spec: return plan
    fixed = {k:spec[k] for k in ('batch_id','created','label')}; fixed['preview_digest'] = plan['digest']
    with closing(sqlite3.connect(path)) as db:
        previous = db.execute('SELECT payload,digest FROM jst_batches WHERE batch_id=?',(spec['batch_id'],)).fetchone() if exists(db) else None
        if previous:
            if orders.decode(previous) != fixed: raise wr.Conflict('批次内容不一致，请重新核对')
            out=copy.deepcopy(plan);out['payload'].pop('new_batch');out['digest']=orders.digest(out['payload']);return out
    checkpoint(cancel); files = []
    for wh,b in spec['stocks'].items():
        c = b['catalog']; source = Path(c.get('source_path',''))
        if source.is_file():
            raw = source.read_bytes(); checkpoint(cancel)
            if hashlib.sha256(raw).hexdigest() != c['source_sha256']: raise ValueError(wh+'资料在准备后变化，请重新预览')
            files.append((c['source_sha256'], source.name, raw))
    storage_chunks.backup_before_write(Path(path), progress)
    with closing(sqlite3.connect(path, timeout=.1)) as db,db:
        lc.begin(db,cancel); schema(db)
        previous = db.execute('SELECT payload,digest FROM jst_batches WHERE batch_id=?', (spec['batch_id'],)).fetchone()
        fixed = {k:spec[k] for k in ('batch_id','created','label')}; fixed['preview_digest'] = plan['digest']
        if previous:
            if orders.decode(previous) != fixed: raise wr.Conflict('批次内容不一致，请重新核对')
        else:
            for p in v['plans']:
                h = p['head']; orders.purchases.validate_head(db,h)
                if orders.active(db,h) or orders.locked(db,h) or lc.current(db,h,'payment') or lc.current(db,h,'receipt'):
                    raise wr.Conflict('订单已生成、已下单或已有下游资料：'+h['number'])
                if db.execute("SELECT 1 FROM jst_batch_members WHERE oid=? AND warehouse=? AND status IN ('pending','failed')", (h['oid'],h['warehouse'])).fetchone():
                    raise wr.Conflict('订单已加入其他未完成批次：'+h['number'])
                full = {**h,'products':p.get('signed_products',p['basis']['products'])}
                if lc.upstream(db,full,'payment') != p['basis']: raise wr.Conflict('回传依据已变化：'+h['number'])
            _pause_other(db,spec['batch_id'])
            db.execute('INSERT INTO jst_batches VALUES(?,?,?,?,?,?,?)', (spec['batch_id'],spec['created'],spec['label'],'active',0,wr.dumps(fixed),orders.digest(fixed)))
            for wh,b in spec['stocks'].items():
                db.execute('INSERT INTO jst_baselines VALUES(?,?,?,?,?)', (b['bid'],wh,b['created'],wr.dumps(b),orders.digest(b)))
                db.execute('INSERT INTO jst_batch_stock VALUES(?,?,?,0)', (spec['batch_id'],wh,b['bid']))
            db.executemany('INSERT INTO jst_files VALUES(?,?,?) ON CONFLICT(sha) DO NOTHING', files)
            for m in spec['members']:
                h = m['head']; db.execute('INSERT INTO jst_batch_members VALUES(?,?,?,?,?,?,?)', (spec['batch_id'],m['position'],h['oid'],h['warehouse'],wr.dumps(h),'pending','等待本批分配'))
            orders.event(db,{},'建立独立分配批次',{'batch_id':spec['batch_id'],'label':spec['label']})
        checkpoint(cancel); progress(CommitProgress(True)); db.commit()
    progress(CommitProgress(False))
    out = copy.deepcopy(plan); out['payload'].pop('new_batch'); out['digest'] = orders.digest(out['payload']); return out


def validate_member(db, p, *, replay=False):
    bid = p['batch_id']; row = db.execute('SELECT status,epoch FROM jst_batches WHERE batch_id=?',(bid,)).fetchone()
    member = db.execute('SELECT status FROM jst_batch_members WHERE batch_id=? AND oid=? AND warehouse=?', (bid,p['head']['oid'],p['head']['warehouse'])).fetchone()
    if not row or not member or member[0]=='cancelled': raise wr.Conflict('本批任务已撤销或不存在')
    if not replay and (row[0]!='active' or row[1]!=p['batch_epoch']): raise wr.Conflict('本批已暂停或任务状态变化，请继续原批次')
    if not replay and member[0] in ('pending','failed') and db.execute("SELECT 1 FROM jst_batch_members WHERE batch_id=? AND warehouse=? AND position<? AND status IN ('pending','failed')",(bid,p['head']['warehouse'],p['position'])).fetchone():
        raise wr.Conflict('该仓前序任务未完成，请按原顺序重试')


def record(db, p):
    if not p.get('batch_id'): return
    if p['summary']['positive']:
        db.execute('INSERT INTO jst_generation_batches VALUES(?,?,?)', (p['gid'],p['batch_id'],p['position']))
    db.execute("UPDATE jst_batch_members SET status='done',message=? WHERE batch_id=? AND oid=? AND warehouse=?", ('已生成，可导出' if p['summary']['positive'] else '无可分配库存',p['batch_id'],p['head']['oid'],p['head']['warehouse']))
    settle_tasks(db,p['head'],'done','本批已完成分配')


def settle_tasks(db, head, status, message):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='jst_tasks'").fetchone():
        db.execute("UPDATE jst_tasks SET status=?,message=? WHERE json_extract(head,'$.oid')=? AND json_extract(head,'$.warehouse')=?", (status,message,head['oid'],head['warehouse']))


def cancel_member(db, head, gid=None):
    settle_tasks(db,head,'cancelled','业务已撤销；不会自动重新生成')
    if not exists(db): return
    rows = db.execute("SELECT batch_id,warehouse FROM jst_batch_members WHERE oid=? AND warehouse=? AND status<>'cancelled'",(head['oid'],head['warehouse'])).fetchall()
    for bid,wh in rows:
        if gid and generation_batch(db,gid) not in (None,bid): continue
        db.execute("UPDATE jst_batch_members SET status='cancelled',message='业务明确撤销' WHERE batch_id=? AND oid=? AND warehouse=?", (bid,head['oid'],wh))
        db.execute('UPDATE jst_batch_stock SET serial=serial+1 WHERE batch_id=? AND warehouse=?',(bid,wh))


def pause(path, batch_id, progress=lambda _:None, cancel=None):
    with closing(sqlite3.connect(path,timeout=.1)) as db,db:
        lc.begin(db,cancel)
        db.execute("UPDATE jst_batches SET status='paused',epoch=epoch+1 WHERE batch_id=? AND status='active'",(batch_id,))


def cancel_unfinished(path,batch_id,reason,progress=lambda _:None,cancel=None):
    """Cancel only pending work; generated/locked results remain available."""
    if not reason.strip():raise ValueError('请填写撤销原因')
    storage_chunks.backup_before_write(Path(path),progress)
    with closing(sqlite3.connect(path,timeout=.1)) as db,db:
        lc.begin(db,cancel);c=context(db,batch_id,load_stock=False);n=0
        for m in c['members']:
            checkpoint(cancel)
            if m['status'] not in ('pending','failed'):continue
            h=m['head'];settle_tasks(db,h,'cancelled','业务撤销未完成任务：'+reason)
            db.execute("UPDATE jst_batch_members SET status='cancelled',message=? WHERE batch_id=? AND position=?",(reason,batch_id,m['position']));n+=1
        db.execute("UPDATE jst_batches SET status='complete',epoch=epoch+1 WHERE batch_id=?",(batch_id,))
        orders.event(db,{},'撤销批次未完成任务',{'batch_id':batch_id,'reason':reason,'count':n})
        checkpoint(cancel)
    return n


def deleting_order(db,oid,warehouse=None):
    """Called in the existing permanent-delete transaction, before rows vanish."""
    if not exists(db):return
    db.execute('CREATE TABLE IF NOT EXISTS jst_batch_retired(batch_id TEXT,warehouse TEXT,sku TEXT,quantity TEXT,PRIMARY KEY(batch_id,warehouse,sku))')
    condition=' AND warehouse=?' if warehouse is not None else ''
    args=(oid,warehouse) if warehouse is not None else (oid,)
    members=db.execute('SELECT batch_id,warehouse FROM jst_batch_members WHERE oid=?'+condition,args).fetchall()
    for bid,wh in members:
        h={'oid':oid,'warehouse':wh};gid=orders.active(db,h)
        if gid and generation_batch(db,gid)==bid and orders.locked(db,h):
            for sku,qty in db.execute('SELECT sku,quantity FROM jst_reservations WHERE gid=?',(gid,)):
                old=db.execute('SELECT quantity FROM jst_batch_retired WHERE batch_id=? AND warehouse=? AND sku=?',(bid,wh,sku)).fetchone()
                db.execute('INSERT OR REPLACE INTO jst_batch_retired VALUES(?,?,?,?)',(bid,wh,sku,str(Decimal(qty)+(Decimal(old[0]) if old else 0))))
        # Removing an unlocked member only releases capacity; a locked member's
        # reservation was transferred to retired stock above. Neither decreases
        # available stock or changes another member's frozen allocation.
        # Keep the surviving batch's execution epoch and control state.
    db.execute('DELETE FROM jst_generation_batches WHERE gid IN (SELECT gid FROM jst_generations WHERE oid=?'+condition+')',args)
    db.execute('DELETE FROM jst_batch_members WHERE oid=?'+condition,args)
    for bid,wh in members:
        if not db.execute('SELECT 1 FROM jst_batch_members WHERE batch_id=?',(bid,)).fetchone():
            db.execute('DELETE FROM jst_batch_stock WHERE batch_id=?',(bid,));db.execute('DELETE FROM jst_batch_retired WHERE batch_id=?',(bid,));db.execute('DELETE FROM jst_batches WHERE batch_id=?',(bid,))


def activate(path, plan, progress=lambda _:None, cancel=None):
    """Explicit resume only; return a preview bound to the new control epoch."""
    out = copy.deepcopy(plan); v = out['payload']; bid = v['batch_id']
    with closing(sqlite3.connect(path,timeout=.1)) as db,db:
        lc.begin(db,cancel); c = context(db,bid,load_stock=False)
        if c['epoch'] != v['batch_epoch']: raise wr.Conflict('批次状态已变化，请重新核对')
        for wh,expected in v['states'].items():
            if state(db,bid,wh) != tuple(expected): raise wr.Conflict('本批占用已变化，请重新预览')
        _pause_other(db,bid)
        epoch = c['epoch'] + (c['status'] != 'active')
        db.execute("UPDATE jst_batches SET status='active',epoch=? WHERE batch_id=?",(epoch,bid))
    v['batch_epoch'] = epoch
    for p in v['plans']: p['batch_epoch'] = epoch
    out['digest'] = orders.digest(v); return out


def execute(path, plan, accept_issues=False, progress=lambda _:None, cancel=None, *, background=False):
    bid=plan['payload'].get('batch_id')
    try:return _execute(path,plan,accept_issues,progress,cancel,background=background)
    except BaseException:
        # Registration may have committed before cancellation. Persist the stop
        # so a later timer/restart cannot consume this batch unexpectedly.
        with closing(sqlite3.connect(path,timeout=10)) as db,db:
            if exists(db):db.execute("UPDATE jst_batches SET status='paused',epoch=epoch+1 WHERE batch_id=? AND status='active' AND epoch IN (?,?)",(bid,plan['payload'].get('batch_epoch',0),plan['payload'].get('batch_epoch',0)+1))
        raise


def _execute(path, plan, accept_issues=False, progress=lambda _:None, cancel=None, *, background=False):
    checkpoint(cancel)
    if orders.digest(plan['payload']) != plan['digest']: raise ValueError('分配预览被修改')
    if any(p['summary']['issues'] for p in plan['payload']['plans']) and not accept_issues: raise ValueError('请核对待核商品并明确本次不安排')
    prepared = register(path,plan,progress,cancel); bid = prepared['payload']['batch_id']
    with closing(sqlite3.connect(path)) as db:
        replay = bool(prepared['payload']['plans']) and all(db.execute('SELECT 1 FROM jst_steps WHERE operation=?',(p['operation'],)).fetchone() for p in prepared['payload']['plans'])
    if not replay and not background: prepared = activate(path,prepared,progress,cancel)
    receipt = orders.commit(path,prepared,accept_issues,progress,cancel)
    with closing(sqlite3.connect(path,timeout=.1)) as db,db:
        lc.begin(db,None)
        # Do not overwrite a newer user's pause/activation decision.
        c = db.execute('SELECT epoch FROM jst_batches WHERE batch_id=?',(bid,)).fetchone()
        if c and c[0] == prepared['payload']['batch_epoch']:
            remaining = db.execute("SELECT count(*) FROM jst_batch_members WHERE batch_id=? AND status IN ('pending','failed')",(bid,)).fetchone()[0]
            if receipt['errors'] or receipt['cancelled']:
                db.execute("UPDATE jst_batches SET status='paused',epoch=epoch+1 WHERE batch_id=?",(bid,))
            elif not remaining: db.execute("UPDATE jst_batches SET status='complete' WHERE batch_id=?",(bid,))
    receipt['batch_id'] = bid; return receipt


def continue_batch(path,batch_id,progress=lambda _:None,cancel=None):
    return execute(path,preview_existing(path,batch_id,progress=progress,cancel=cancel),progress=progress,cancel=cancel)


def drain(path,progress=lambda _:None,cancel=None,*,before_work=lambda:None):
    with closing(sqlite3.connect(path)) as db:
        if not exists(db):checkpoint(cancel);return []
        ids = [r[0] for r in db.execute("SELECT batch_id FROM jst_batches WHERE status='active' ORDER BY rowid")]
    out = []
    checkpoint(cancel) if not ids else None
    for bid in ids:
        try:
            checkpoint(cancel)
            before_work()
            plan = preview_existing(path,bid,progress=progress,cancel=cancel)
            out.append(execute(path,plan,progress=progress,cancel=cancel,background=True))
        except Cancelled:
            pause(path,bid);raise
        except Exception as exc:
            pause(path,bid); out.append({'batch_id':bid,'error':str(exc)})
    return out
