"""Explicit allocation batches. Frozen stock, scoped reservations, resumable order.

The numeric allocator remains jst_orders/jst_allocation. Legacy generations are
read as recorded; neither dates nor file hashes are used to infer old batches.
"""
import copy
import hashlib
import sqlite3
import uuid
from contextlib import ExitStack,closing
from decimal import Decimal
from pathlib import Path

import jst_orders as orders
from jst_batch_store import ZERO_OUTCOME, context, exists, generation_batch, listing, state
import order_lifecycle as lc
import work_repository as wr
import warehouse_catalog as wc
import storage_chunks
from runtime_support import checkpoint, Cancelled, CommitProgress


def schema(db):
    orders.schema(db)
    for sql in (
        'CREATE TABLE IF NOT EXISTS jst_batches(batch_id TEXT PRIMARY KEY,created TEXT,label TEXT,status TEXT,epoch INTEGER,payload TEXT,digest TEXT)',
        'CREATE TABLE IF NOT EXISTS jst_batch_stock(batch_id TEXT,warehouse TEXT,bid TEXT,serial INTEGER,PRIMARY KEY(batch_id,warehouse))',
        'CREATE TABLE IF NOT EXISTS jst_batch_members(batch_id TEXT,position INTEGER,oid TEXT,warehouse TEXT,head TEXT,status TEXT,message TEXT,PRIMARY KEY(batch_id,position),UNIQUE(batch_id,oid,warehouse))',
        'CREATE TABLE IF NOT EXISTS jst_generation_batches(gid TEXT PRIMARY KEY,batch_id TEXT,position INTEGER)',
        'CREATE INDEX IF NOT EXISTS jst_batch_member_order ON jst_batch_members(oid,warehouse)',
    ): db.execute(sql)


def reservations(db, batch_id, warehouse, exclude=()):
    values = {}; exclude = set(exclude)
    if not exists(db): return values
    rows = db.execute('''SELECT a.gid,r.sku,r.quantity FROM jst_active a
        JOIN jst_generation_batches b ON b.gid=a.gid JOIN jst_reservations r ON r.gid=a.gid
        WHERE b.batch_id=? AND a.warehouse=? ORDER BY b.position,r.rowid''', (batch_id, warehouse))
    for gid, sku, qty in rows:
        if gid not in exclude: values[sku] = values.get(sku, Decimal(0)) + Decimal(qty)
    # Locally deleted orders with execution evidence still consume this batch.
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='jst_batch_retired'").fetchone():
        for sku, qty in db.execute('SELECT sku,quantity FROM jst_batch_retired WHERE batch_id=? AND warehouse=?', (batch_id, warehouse)):
            values[sku] = values.get(sku, Decimal(0)) + Decimal(qty)
    return values


def _unfinished(db, heads, cancel=None):
    """Exact selected claims plus the explicitly reviewable original batch scope."""
    if not exists(db): return []
    selected = {(h['oid'], h['warehouse']) for h in heads}; groups = []; found = {}
    for oid,wh in selected:
        checkpoint(cancel)
        for row in db.execute("SELECT b.rowid,b.batch_id,b.label,b.status,b.epoch FROM jst_batch_members m JOIN jst_batches b ON b.batch_id=m.batch_id WHERE m.oid=? AND m.warehouse=? AND m.status IN ('pending','failed')",(oid,wh)):
            found[row[1]]=row
    for _, bid, label, status, epoch in sorted(found.values()):
        checkpoint(cancel)
        remaining = [dict(position=p, head=wr.loads(h), status=s, message=m) for p,h,s,m in db.execute(
            "SELECT position,head,status,message FROM jst_batch_members WHERE batch_id=? AND status IN ('pending','failed') ORDER BY position", (bid,))]
        chosen = [m for m in remaining if (m['head']['oid'],m['head']['warehouse']) in selected]
        if chosen: groups.append(dict(batch_id=bid,label=label,status=status,epoch=epoch,selected=chosen,remaining=remaining))
    return groups


def unfinished(path, heads, progress=lambda _:None, cancel=None):
    """Read only. Never infer that a pending batch is safe to discard."""
    with closing(sqlite3.connect(path)) as db: return _unfinished(db,heads,cancel)


def release_unfinished(path, groups, progress=lambda _:None, cancel=None):
    """Explicitly end selected pending claims; preserve all allocations and others."""
    if not groups: return 0
    heads = [m['head'] for g in groups for m in g['selected']]
    storage_chunks.backup_before_write(Path(path),progress)
    with closing(sqlite3.connect(path,timeout=.1)) as db,db:
        lc.begin(db,cancel)
        # Recheck the whole displayed scope after obtaining the write lock.
        # A late commit, pause, deletion or replacement requires a fresh review.
        if _unfinished(db,heads,cancel) != groups:
            raise wr.Conflict('原批任务已变化，未结束任何任务；请重新预览并核对')
        count = 0
        for g in groups:
            for m in g['selected']:
                checkpoint(cancel); h=m['head']
                db.execute("UPDATE jst_batch_members SET status='cancelled',message='业务结束本次所选未完成项，改用新批资料' WHERE batch_id=? AND position=?",(g['batch_id'],m['position']))
                settle_tasks(db,h,'cancelled','业务结束所选未完成项；不会由旧批自动恢复')
                count += 1
            pending = db.execute("SELECT 1 FROM jst_batch_members WHERE batch_id=? AND status IN ('pending','failed')",(g['batch_id'],)).fetchone()
            db.execute('UPDATE jst_batches SET status=?,epoch=epoch+1 WHERE batch_id=?',('paused' if pending else 'complete',g['batch_id']))
            orders.event(db,{},'结束所选批次未完成项',{'batch_id':g['batch_id'],'orders':[m['head'] for m in g['selected']]})
        checkpoint(cancel); progress(CommitProgress(True)); db.commit()
    progress(CommitProgress(False))
    return count


def preview_new(path, heads, catalogs=None, manual_order=False, progress=lambda _: None, cancel=None, *, stock_receipts=None):
    """No writes/reservations; selecting the same file explicitly is allowed."""
    checkpoint(cancel); stocks = {}; states = {}; batch_id = uuid.uuid4().hex
    with closing(sqlite3.connect(path)) as db:
        for h in heads:
            if exists(db) and db.execute("SELECT 1 FROM jst_batch_members WHERE oid=? AND warehouse=? AND status IN ('pending','failed')", (h['oid'], h['warehouse'])).fetchone():
                raise wr.Conflict('所选订单已有未完成批次，请继续原批或明确撤销：' + h['number'])
    warehouses=list(dict.fromkeys(h['warehouse'] for h in heads))
    versions={};current_catalogs=None
    if catalogs is None:
        current_catalogs,versions=wc.Repository(Path(path).parent).snapshots()
        versions={wh:versions[wh] for wh in warehouses}
        catalogs=current_catalogs
    if stock_receipts is not None:
        if current_catalogs is None or set(stock_receipts) != set(warehouses):
            raise ValueError('本批缺少对应仓的本次库存刷新回执')
        for wh, receipt in stock_receipts.items():
            current=current_catalogs.get(wh,{})
            if any(current.get(k)!=receipt[k] for k in ('revision','source_sha256')):
                raise wr.Conflict(wh+'当前仓资料与本次刷新回执不同，请重新核对')
    for wh in warehouses:
        if wh not in catalogs: raise ValueError(wh + '尚未选用商品资料')
        checkpoint(cancel); c = wc.validate(copy.deepcopy(catalogs[wh])); bid = uuid.uuid4().hex
        if c['warehouse'] != wh: raise ValueError('选用资料与供货仓不一致')
        stocks[wh] = dict(bid=bid, warehouse=wh, catalog=c, included=[], created=wr.now(), batch_id=batch_id)
        states[wh] = (bid, 0)
    from material_stock import has_material, included
    with closing(wr.read_connection(path)) as db:
        if has_material(db):
            for wh, base in stocks.items():
                base['included'] = sorted(included(db,wh,base))
    spec = dict(batch_id=batch_id, created=wr.now(), epoch=0, stocks=stocks, states=states, members=[], new=True)
    if versions:spec['current_versions']=versions
    if stock_receipts is not None:spec['keep_other_batches']=True
    plan = orders.prepare_checked(path, heads, manual_order, progress=progress, cancel=cancel, batch=spec,current_catalogs=current_catalogs)
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


def _pause_other(db, batch_id, operation=None):
    before=db.execute("SELECT batch_id,epoch FROM jst_batches WHERE batch_id<>? AND status='active' ORDER BY rowid",(batch_id,)).fetchall()
    db.execute("UPDATE jst_batches SET status='paused',epoch=epoch+1 WHERE batch_id<>? AND status='active'", (batch_id,))
    if before and operation:
        orders.event(db,{},'分配控制切换',{'batch_id':batch_id,'operation':operation,
            'paused':[{'batch_id':bid,'epoch':epoch+1} for bid,epoch in before]})


def _restore_switch(db, batch_id, operation):
    # Only undo this operation's control changes, never allocations or a newer
    # pause/resume decision. Called under the same failure-cleanup write lock.
    if db.execute('SELECT 1 FROM jst_steps WHERE runid=?',(operation,)).fetchone():return
    from deletion_barrier import has
    if has(db,'deleted_jst_steps') and db.execute('SELECT 1 FROM deleted_jst_steps WHERE runid=?',(operation,)).fetchone():return
    if db.execute("SELECT 1 FROM jst_batches WHERE batch_id<>? AND status='active'",(batch_id,)).fetchone():return
    rows=db.execute("SELECT detail FROM jst_events WHERE action='分配控制切换' AND json_extract(detail,'$.operation')=? AND json_extract(detail,'$.batch_id')=? ORDER BY id DESC",(operation,batch_id)).fetchall()
    for row in rows:
        for old in wr.loads(row[0])['paused']:
            # Keep epochs monotonic so an old in-flight plan cannot revive.
            if db.execute("SELECT 1 FROM jst_batch_members WHERE batch_id=? AND status IN ('pending','failed')",(old['batch_id'],)).fetchone():
                changed=db.execute("UPDATE jst_batches SET status='active',epoch=epoch+1 WHERE batch_id=? AND status='paused' AND epoch=?",(old['batch_id'],old['epoch'])).rowcount
                if changed:
                    orders.event(db,{},'恢复分配前控制状态',{'batch_id':batch_id,'operation':operation,'restored':old['batch_id']})
                    return


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
    versions=spec.get('current_versions',{})
    from catalog_write_lock import acquire
    catalog_repository=wc.Repository(Path(path).parent)
    with ExitStack() as locks,closing(sqlite3.connect(path, timeout=.1)) as db,db:
        for wh in wc.WAREHOUSES:
            if wh in versions:locks.enter_context(acquire(catalog_repository.folder,wh,cancel))
        lc.begin(db,cancel); schema(db)
        previous = db.execute('SELECT payload,digest FROM jst_batches WHERE batch_id=?', (spec['batch_id'],)).fetchone()
        fixed = {k:spec[k] for k in ('batch_id','created','label')}; fixed['preview_digest'] = plan['digest']
        if previous:
            if orders.decode(previous) != fixed: raise wr.Conflict('批次内容不一致，请重新核对')
        else:
            for wh,version in versions.items():
                if catalog_repository.current_digest(wh)!=version:
                    raise wr.Conflict(wh+'当前仓资料已刷新，请重新预览库存分配')
            for p in v['plans']:
                h = p['head']; orders.purchases.validate_head(db,h)
                blocker, payment = orders.allocation_gate(db,h,orders.active(db,h))
                if blocker or payment != p.get('previous_payment'):
                    raise wr.Conflict((blocker or '付款或重做依据已变化')+'：'+h['number'])
                if db.execute("SELECT 1 FROM jst_batch_members WHERE oid=? AND warehouse=? AND status IN ('pending','failed')", (h['oid'],h['warehouse'])).fetchone():
                    raise wr.Conflict('订单已加入其他未完成批次：'+h['number'])
                full = {**h,'products':p.get('signed_products',p['basis']['products'])}
                if lc.upstream(db,full,'payment') != p['basis']: raise wr.Conflict('回传依据已变化：'+h['number'])
            if spec.get('keep_other_batches') and db.execute(
                    "SELECT 1 FROM jst_batches WHERE status='active' LIMIT 1").fetchone():
                raise wr.Conflict('另有分配批次正在处理，请等原批结束后再开始一键作业')
            _pause_other(db,spec['batch_id'],v['runid'])
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
    amendment=p.get('manual_correction') and member[0]=='done' and row[0] in ('active','paused','complete')
    if not replay and (row[1]!=p['batch_epoch'] or (row[0]!='active' and not amendment)):
        raise wr.Conflict('本批已暂停或任务状态变化，请继续原批次')
    if not replay and member[0] in ('pending','failed') and db.execute("SELECT 1 FROM jst_batch_members WHERE batch_id=? AND warehouse=? AND position<? AND status IN ('pending','failed')",(bid,p['head']['warehouse'],p['position'])).fetchone():
        raise wr.Conflict('该仓前序任务未完成，请按原顺序重试')


def record(db, p):
    if not p.get('batch_id'): return
    if p['summary']['positive']:
        db.execute('INSERT INTO jst_generation_batches VALUES(?,?,?)', (p['gid'],p['batch_id'],p['position']))
    db.execute("UPDATE jst_batch_members SET status='done',message=? WHERE batch_id=? AND oid=? AND warehouse=?", ('已生成，可导出' if p['summary']['positive'] else ZERO_OUTCOME,p['batch_id'],p['head']['oid'],p['head']['warehouse']))
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
        db.execute("UPDATE jst_batches SET status='paused',epoch=epoch+1 WHERE batch_id=? AND status IN ('active','paused')",(batch_id,))


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


def deleting_order(db,oid,warehouse=None,executed=()):
    """Called in the existing permanent-delete transaction, before rows vanish."""
    if not exists(db):return
    db.execute('CREATE TABLE IF NOT EXISTS jst_batch_retired(batch_id TEXT,warehouse TEXT,sku TEXT,quantity TEXT,PRIMARY KEY(batch_id,warehouse,sku))')
    condition=' AND warehouse=?' if warehouse is not None else ''
    args=(oid,warehouse) if warehouse is not None else (oid,)
    members=db.execute('SELECT batch_id,warehouse,status FROM jst_batch_members WHERE oid=?'+condition,args).fetchall()
    for bid,wh,status in members:
        h={'oid':oid,'warehouse':wh};gid=orders.active(db,h)
        owns_allocation = gid and generation_batch(db, gid) == bid
        keep = owns_allocation and ((oid, wh) in executed or orders.locked(db,h))
        if keep:
            from material_stock import retain_retired
            retain_retired(db,oid,wh,gid)
            for sku,qty in db.execute('SELECT sku,quantity FROM jst_reservations WHERE gid=?',(gid,)):
                old=db.execute('SELECT quantity FROM jst_batch_retired WHERE batch_id=? AND warehouse=? AND sku=?',(bid,wh,sku)).fetchone()
                db.execute('INSERT OR REPLACE INTO jst_batch_retired VALUES(?,?,?,?)',(bid,wh,sku,str(Decimal(qty)+(Decimal(old[0]) if old else 0))))
        if status in ('pending', 'failed') or (owns_allocation and not keep):
            # A preview may already include the removed member's trial quantities.
            # Preserve other warehouses' serials and already committed steps.
            db.execute('UPDATE jst_batch_stock SET serial=serial+1 WHERE batch_id=? AND warehouse=?',(bid,wh))
    db.execute('DELETE FROM jst_generation_batches WHERE gid IN (SELECT gid FROM jst_generations WHERE oid=?'+condition+')',args)
    db.execute('DELETE FROM jst_batch_members WHERE oid=?'+condition,args)
    for bid,wh,_ in members:
        if not db.execute('SELECT 1 FROM jst_batch_members WHERE batch_id=?',(bid,)).fetchone():
            db.execute('DELETE FROM jst_batch_stock WHERE batch_id=?',(bid,));db.execute('DELETE FROM jst_batch_retired WHERE batch_id=?',(bid,));db.execute('DELETE FROM jst_batches WHERE batch_id=?',(bid,))


def deleting_orders(db, selection, cancel=None, *, executed=()):
    """Prune JST work in the caller's deletion transaction. None selects all warehouses."""
    selection = tuple(selection)
    selected = set(selection)
    def matches(head):
        oid = head.get('oid')
        return (oid, None) in selected or (oid, head.get('warehouse')) in selected

    tables = {name for name, in db.execute("SELECT name FROM sqlite_master WHERE name IN ('jst_active','jst_tasks')")}
    if 'jst_active' in tables:
        db.execute('CREATE TABLE IF NOT EXISTS retired_stock(warehouse TEXT,bid TEXT,sku TEXT,quantity TEXT,PRIMARY KEY(warehouse,bid,sku))')
        for oid, warehouse in selection:
            checkpoint(cancel)
            condition = ' AND warehouse=?' if warehouse is not None else ''
            args = (oid, warehouse) if warehouse is not None else (oid,)
            active = db.execute('SELECT warehouse,gid FROM jst_active WHERE oid=?'+condition, args).fetchall()
            for wh, gid in active:
                baseline = None if generation_batch(db,gid) else orders.state(db,wh)
                if not baseline or not orders.locked(db,{'oid':oid,'warehouse':wh}):
                    continue
                from material_stock import retain_retired
                retain_retired(db,oid,wh,gid)
                bid = baseline[0]
                raw = db.execute('SELECT payload FROM jst_baselines WHERE bid=?',(bid,)).fetchone()[0]
                if gid in wr.loads(raw).get('included',[]):
                    continue
                for sku, qty in db.execute('SELECT sku,quantity FROM jst_reservations WHERE gid=?',(gid,)):
                    old = db.execute('SELECT quantity FROM retired_stock WHERE warehouse=? AND bid=? AND sku=?',(wh,bid,sku)).fetchone()
                    total = Decimal(qty) + (Decimal(old[0]) if old else 0)
                    db.execute('INSERT OR REPLACE INTO retired_stock VALUES(?,?,?,?)',(wh,bid,sku,str(total)))
            deleting_order(db,oid,warehouse,executed)
            for gid, in db.execute('SELECT gid FROM jst_generations WHERE oid=?'+condition,args).fetchall():
                db.execute('DELETE FROM jst_headers WHERE gid=?',(gid,))
                db.execute('DELETE FROM jst_reservations WHERE gid=?',(gid,))

        for runid, raw in db.execute('SELECT runid,payload FROM jst_runs').fetchall():
            checkpoint(cancel)
            payload = wr.loads(raw)
            removed = [p for p in payload['plans'] if matches(p['head'])]
            previous_skipped = payload.get('skipped',[])
            skipped = [p for p in previous_skipped if not matches(p.get('head',{}))]
            if not removed and skipped == previous_skipped:
                continue
            payload['plans'] = [p for p in payload['plans'] if not matches(p['head'])]
            payload['skipped'] = skipped
            for plan in removed:
                operation, wh = plan['operation'], plan['head']['warehouse']
                step = db.execute('SELECT runid FROM jst_steps WHERE operation=?',(operation,)).fetchone()
                if step:
                    db.execute('INSERT OR IGNORE INTO deleted_jst_steps VALUES(?,?,?,?)',(operation,step[0],wh,payload['states'][wh][0]))
                db.execute('DELETE FROM jst_steps WHERE operation=?',(operation,))
            if payload['plans'] or skipped:
                db.execute('UPDATE jst_runs SET payload=?,digest=? WHERE runid=?',(wr.dumps(payload),orders.digest(payload),runid))
            else:
                db.execute('DELETE FROM jst_runs WHERE runid=?',(runid,))

    if 'jst_tasks' in tables:
        for task, raw in db.execute('SELECT task,head FROM jst_tasks').fetchall():
            checkpoint(cancel)
            if matches(wr.loads(raw)):
                db.execute('DELETE FROM jst_tasks WHERE task=?',(task,))


def activate(path, plan, progress=lambda _:None, cancel=None):
    """Explicit resume only; return a preview bound to the new control epoch."""
    out = copy.deepcopy(plan); v = out['payload']; bid = v['batch_id']
    with closing(sqlite3.connect(path,timeout=.1)) as db,db:
        lc.begin(db,cancel); c = context(db,bid,load_stock=False)
        if c['epoch'] != v['batch_epoch']: raise wr.Conflict('批次状态已变化，请重新核对')
        for wh,expected in v['states'].items():
            if state(db,bid,wh) != tuple(expected): raise wr.Conflict('本批占用已变化，请重新预览')
        _pause_other(db,bid,v['runid'])
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
        try:
            with closing(sqlite3.connect(path,timeout=.1)) as db,db:
                if exists(db):
                    lc.begin(db,None)
                    changed=db.execute("UPDATE jst_batches SET status='paused',epoch=epoch+1 WHERE batch_id=? AND status='active' AND epoch IN (?,?)",(bid,plan['payload'].get('batch_epoch',0),plan['payload'].get('batch_epoch',0)+1)).rowcount
                    if changed:_restore_switch(db,bid,plan['payload']['runid'])
        except Exception:
            import logging
            logging.exception('分配异常后控制状态待恢复；保留原始异常及批次 %s',bid)
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
    receipt['batch_id']=bid
    receipt['commit_state']='partial' if receipt['errors'] or receipt['cancelled'] else 'committed'
    try:
        with closing(sqlite3.connect(path,timeout=.1)) as db,db:
            lc.begin(db,None)
            # Do not overwrite a newer user's pause/activation decision.
            c = db.execute('SELECT epoch FROM jst_batches WHERE batch_id=?',(bid,)).fetchone()
            if c and c[0] == prepared['payload']['batch_epoch']:
                remaining = db.execute("SELECT count(*) FROM jst_batch_members WHERE batch_id=? AND status IN ('pending','failed')",(bid,)).fetchone()[0]
                if receipt['errors'] or receipt['cancelled']:
                    db.execute("UPDATE jst_batches SET status='paused',epoch=epoch+1 WHERE batch_id=?",(bid,))
                    _restore_switch(db,bid,prepared['payload']['runid'])
                elif not remaining: db.execute("UPDATE jst_batches SET status='complete' WHERE batch_id=?",(bid,))
    except Exception as exc:
        import logging
        logging.exception('分配业务回执已取得，批次收尾待恢复：%s',bid)
        receipt.update(finalization_pending=True,recovery_warning='已保存的分配结果保留，批次状态待恢复：'+type(exc).__name__+'：'+str(exc))
    return receipt


def recover_batch(path,batch_id,progress=lambda _:None,cancel=None):
    """Read committed member facts and repair control metadata, never allocate."""
    with closing(sqlite3.connect(path,timeout=.1)) as db,db:
        lc.begin(db,cancel);c=context(db,batch_id,load_stock=False)
        generated=[];zero=[];pending=[];errors=[]
        for m in c['members']:
            checkpoint(cancel)
            head=m['head']
            if m['status']=='done':
                if m['message']==ZERO_OUTCOME:
                    zero.append(head['number'])
                    continue
                gid=orders.active(db,head)
                if gid and generation_batch(db,gid)==batch_id:
                    generated.append(gid)
                else:
                    errors.append({'orders':[head['number']],'error':'本批已完成项缺少对应的有效生成记录，请核对原批'})
            elif m['status'] in ('pending','failed'):pending.append(head['number'])
        if not pending:
            db.execute("UPDATE jst_batches SET status='complete' WHERE batch_id=?",(batch_id,))
        elif c['status']=='active':
            # Explicit recovery stops stale dispatch before a new preview.
            db.execute("UPDATE jst_batches SET status='paused',epoch=epoch+1 WHERE batch_id=?",(batch_id,))
        if pending and not generated and not zero:
            last=db.execute("SELECT json_extract(detail,'$.operation') FROM jst_events WHERE action='分配控制切换' AND json_extract(detail,'$.batch_id')=? ORDER BY id DESC LIMIT 1",(batch_id,)).fetchone()
            if last:_restore_switch(db,batch_id,last[0])
        orders.event(db,{},'核对并恢复分配状态',{'batch_id':batch_id,'generated':generated,'zero':zero,'pending':pending,'errors':errors})
        checkpoint(cancel)
    followup=f'剩余{len(pending)}单请继续原批，已保存部分不重做' if pending else '已保存结果可在本批核对和导出'
    if errors:followup+='；原批回执待核：'+'、'.join(e['orders'][0] for e in errors)
    return {'batch_id':batch_id,'generated':generated,'zero':zero,'pending':pending,'errors':errors,
            'skipped':[],'cancelled':False,'commit_state':'partial' if pending or errors else 'committed',
            'message':f'已核实保存{len(generated)}单、零分配{len(zero)}单；'+followup}


def continue_batch(path,batch_id,progress=lambda _:None,cancel=None):
    with closing(sqlite3.connect(path)) as db:
        c=context(db,batch_id,load_stock=False)
        remaining=any(m['status'] in ('pending','failed') for m in c['members'])
    if not remaining:return recover_batch(path,batch_id,progress,cancel)
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
            with closing(sqlite3.connect(path)) as db:
                remaining=db.execute("SELECT 1 FROM jst_batch_members WHERE batch_id=? AND status IN ('pending','failed')",(bid,)).fetchone()
            if not remaining:
                out.append(recover_batch(path,bid,progress,cancel));continue
            plan = preview_existing(path,bid,progress=progress,cancel=cancel)
            out.append(execute(path,plan,progress=progress,cancel=cancel,background=True))
        except Cancelled:
            pause(path,bid);raise
        except Exception as exc:
            pause(path,bid); out.append({'batch_id':bid,'error':str(exc)})
    return out
