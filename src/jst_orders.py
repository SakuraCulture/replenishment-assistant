"""Local warehouse baselines, immutable execution orders and atomic reservations.

All new tables are additive. Signed/returned/model quantities are never rewritten.
New warehouse stock already includes orders locked BEFORE that refresh.
"""
import copy
import hashlib
import sqlite3
import time
import uuid
from contextlib import closing
from decimal import Decimal
from pathlib import Path
import work_repository as wr
import order_lifecycle as lc
import purchases
import warehouse_catalog
import storage_chunks
import jst_allocation as allocation
import material_stock
from runtime_support import checkpoint, CommitProgress, Cancelled
from export_progress import RowProgress
from jst_order_guards import allocation_gate, guard_payment, guard_resign, saved_execution_reason
from jst_order_store import active, baseline, decode, digest, event, exists, get, locked, schema, state


def catalogs(path, folder=None, progress=lambda _: None, cancel=None):
    """The same current warehouse data used by replenishment."""
    checkpoint(cancel)
    current=warehouse_catalog.Repository(folder or Path(path).parent).catalogs()
    return {wh:{'catalog':catalog} for wh,catalog in current.items()}


def prepare_refresh(path, warehouse, source=None, catalog=None, operation=None, progress=lambda _: None, cancel=None):
    checkpoint(cancel)
    with closing(sqlite3.connect(path)) as db:
        expected = state(db, warehouse)
        included = [r[0] for r in db.execute('SELECT gid FROM jst_locks WHERE warehouse=? AND locked=1 ORDER BY rowid', (warehouse,))] if exists(db) else []
    c = warehouse_catalog.import_catalog(source, warehouse, progress, cancel) if source else warehouse_catalog.validate(copy.deepcopy(catalog))
    if c['warehouse'] != warehouse:
        raise ValueError('仓资料标签不一致')
    value = {'bid': operation or uuid.uuid4().hex, 'warehouse': warehouse, 'catalog': c,
             'included': included, 'expected': expected, 'created': wr.now()}
    checkpoint(cancel)
    return {'payload': value, 'digest': digest(value)}


def commit_refresh(path, plan, progress=lambda _: None, cancel=None):
    checkpoint(cancel); p = plan['payload']
    if digest(p) != plan['digest']:
        raise ValueError('库存准备结果已被修改')
    raw = None; c = p['catalog']; source = Path(c.get('source_path', ''))
    if source.is_file():
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != c['source_sha256']:
            raise ValueError('仓资料在准备后变化，请重新导入')
    with closing(sqlite3.connect(path)) as check:
        if exists(check):
            hit = check.execute('SELECT payload,digest FROM jst_baselines WHERE bid=?', (p['bid'],)).fetchone()
            if hit:
                if digest(decode(hit)) != plan['digest']:
                    raise ValueError('重复导入操作内容不一致')
                return p['bid']
    storage_chunks.backup_before_write(Path(path), progress); checkpoint(cancel)
    with closing(sqlite3.connect(path, timeout=.1)) as db, db:
        lc.begin(db, cancel); schema(db)
        if state(db, p['warehouse']) != (tuple(p['expected']) if p['expected'] else None):
            raise wr.Conflict('读取期间锁单、分配或仓资料变化，请刷新后重新更新库存')
        locked_ids=[r[0] for r in db.execute('SELECT gid FROM jst_locks WHERE warehouse=? AND locked=1 ORDER BY rowid',(p['warehouse'],))]
        if locked_ids!=p['included']:raise wr.Conflict('读取期间锁单变化，请重新更新库存')
        db.execute('INSERT INTO jst_baselines VALUES(?,?,?,?,?)', (p['bid'], p['warehouse'], p['created'], wr.dumps(p), plan['digest']))
        serial = (p['expected'][1] if p['expected'] else 0) + 1
        db.execute('INSERT INTO jst_warehouses VALUES(?,?,?) ON CONFLICT(warehouse) DO UPDATE SET bid=excluded.bid,serial=excluded.serial', (p['warehouse'], p['bid'], serial))
        if raw is not None:
            db.execute('INSERT OR IGNORE INTO jst_files VALUES(?,?,?)', (c['source_sha256'], source.name, raw))
        event(db, p, '更新仓库存', {'bid': p['bid'], '已包含锁单': len(p['included']), 'source': c.get('source_path')})
        checkpoint(cancel); progress(CommitProgress(True)); db.commit()
    progress(CommitProgress(False)); return p['bid']


def return_basis(db, head):
    purchases.validate_head(db, head)
    basis = lc.upstream(db, head, 'payment')
    stage=lc.flow.demand_stage(db,head)
    if not basis['ready']:
        raise ValueError(lc.TITLES[stage]+'尚未确认或依据已变化，请核对该环节：' + head['number'])
    doc = lc.current(db, head, stage)
    rows = doc['payload']['rows'] if doc else [dict(sku=s, name=p.get('name'), base_quantity=p['quantity'], sources=[]) for s,p in basis['products'].items()]
    legacy = lc.legacy_return(db,head,head['products']) if doc is None else None
    return basis, rows, doc['saved'] if doc else legacy['saved'], copy.deepcopy(head['products']) if doc is None else None


def reservations(db, wh, included, exclude=()):
    values = {}
    sql = 'SELECT a.gid,r.sku,r.quantity FROM jst_active a JOIN jst_reservations r ON a.gid=r.gid WHERE a.warehouse=?'
    if material_stock.shared_ledger(db):
        sql += " AND NOT EXISTS(SELECT 1 FROM normal_orders o WHERE o.oid=a.oid AND o.kind IN ('material','policy','opening'))"
    for gid, sku, qty in db.execute(sql+' ORDER BY a.rowid,r.rowid', (wh,)):
        if gid in included or gid in exclude:
            continue
        values[sku] = values.get(sku, Decimal(0)) + Decimal(qty)
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='retired_stock'").fetchone():
        current=state(db,wh)
        if current:
            for sku,qty in db.execute('SELECT sku,quantity FROM retired_stock WHERE warehouse=? AND bid=?',(wh,current[0])):
                values[sku]=values.get(sku,Decimal(0))+Decimal(qty)
    return values


def prepare_checked(path,heads,manual_order=False,correction=False,progress=lambda _:None,cancel=None,*,batch=None,current_catalogs=None):
    return prepare(path,heads,manual_order,correction,progress,cancel,enforce_admission=True,batch=batch,current_catalogs=current_catalogs)

def prepare(path, heads, manual_order=False, correction=False, progress=lambda _: None, cancel=None,*,enforce_admission=False,batch=None,current_catalogs=None):
    import jst_batches
    runid = uuid.uuid4().hex; orders = []; skipped = []; states = {}; bases = {}; seen = set()
    with closing(sqlite3.connect(path)) as db:
        db.execute('BEGIN')
        for supplied in heads:
            checkpoint(cancel); h = lc.full_head(path, supplied); key = (h['oid'], h['warehouse'])
            if db.execute("SELECT 1 FROM normal_orders WHERE oid=? AND kind IN ('policy','opening')",(h['oid'],)).fetchone():
                raise ValueError('专项执行包请从原工作台按目标行预览和认领，已生成的包可继续原平台任务')
            if key in seen:
                raise ValueError('重复选择同一店仓订单')
            seen.add(key); old = active(db, h)
            blocker,previous_payment=allocation_gate(db,h,old,correction)
            if blocker:
                skipped.append({'head': supplied, 'reason': blocker}); continue
            basis, rows, saved, signed_products = return_basis(db, h)
            if not any(Decimal(r['base_quantity']) > 0 for r in rows) and not (correction and old):
                skipped.append({'head': supplied, 'reason': '无要货，无需生成'}); continue
            wh = h['warehouse']
            if batch and old and jst_batches.generation_batch(db,old)!=batch['batch_id']:
                raise wr.Conflict('所选生成单不属于本批，请进入原批次')
            if wh not in bases:bases[wh]=batch['stocks'].get(wh) if batch else baseline(db,wh)
            b=bases[wh]
            if b is None:
                raise ValueError(wh + '尚未建立库存基准，请导入或采用默认仓资料')
            states[wh] = batch['states'][wh] if batch else state(db, wh)
            compact = {k:v for k,v in h.items() if k != 'products'}
            is_material = material_stock.has_material(db) and material_stock.is_material(db,h['oid'])
            if is_material:
                compact['kind'] = 'material'
            orders.append({'head': compact, 'basis': basis, 'rows': rows, 'saved': saved, 'old': old, 'signed_products':signed_products,'previous_payment':previous_payment,'material':is_material})
        kinds = {o['material'] for o in orders}
        if len(kinds) > 1:
            raise ValueError('物料单和补货单请分别建立库存分配批次')
        material = kinds == {True}
        if correction and not batch:
            # A correction changes subsequent balances; include ALL unlocked active orders in each affected warehouse.
            chosen = {o['old'] for o in orders if o['old']}
            for wh in states:
                outstanding = {g for o,w,g in db.execute('SELECT oid,warehouse,gid FROM jst_active WHERE warehouse=?', (wh,)) if not locked(db, {'oid':o,'warehouse':w})}
                if not outstanding <= chosen:
                    raise ValueError('重新分配须选齐该仓全部未锁已生成订单，避免改变其它订单占用')
        if not manual_order:
            orders.sort(key=lambda o: (o['saved'], o['head']['day'], o['head']['number']))
        balances = {}; reserved = {}; cross = {}
        for wh, b in bases.items():
            exclude={o['old'] for o in orders if correction and o['old']}
            used = jst_batches.reservations(db,batch['batch_id'],wh,exclude) if batch else ({} if material else reservations(db, wh, set(b['included']), exclude))
            wanted = {r['sku'] for o in orders if o['head']['warehouse']==wh for r in o['rows'] if Decimal(r['base_quantity'])>0}
            own_batch = batch['batch_id'] if batch else None
            cross[wh] = material_stock.cross_reservations(db, wh, b, wanted, material=material, batch_id=own_batch, exclude=exclude)
            for sku, qty in cross[wh]['reserved'].items():
                used[sku] = used.get(sku, Decimal(0))+Decimal(qty)
            reserved[wh] = {s:allocation.text(q) for s,q in used.items()}; balances[wh] = {}
            for sku, item in b['catalog']['items'].items():
                try: balances[wh][sku] = allocation.number(item.get('可用数'), '可用库存') - used.get(sku, Decimal(0))
                except ValueError: pass
        plans = []
        if enforce_admission:
            from admission import execution_evidence
            versions=(batch or {}).get('current_versions',{})
            if current_catalogs is None:
                current_catalogs,versions=warehouse_catalog.Repository(Path(path).parent).snapshots()
            defaults={w:c['revision'] for w,c in current_catalogs.items()}
            # A legacy stock pointer is a fallback for inventory, not a published
            # label source for an explicitly selected same-warehouse catalog.
            maintained={w:c for w,c in current_catalogs.items()
                        if w not in bases or not str(versions.get(w)).startswith('legacy:')}
            catalogs={w:base['catalog'] for w,base in bases.items()}
            catalogs.update(maintained)
            admission_sources={w:{'kind':'current_default' if w in maintained else 'explicit_stock_baseline',
                                  'revision':c['revision'],'bid':None if w in maintained else bases[w]['bid']}
                               for w,c in catalogs.items()}
        for i, o in enumerate(orders):
            checkpoint(cancel); wh=o['head']['warehouse']; b=bases[wh]
            evidence=None
            if enforce_admission:
                evidence=execution_evidence(path,o['head']['store'],wh,[r['sku'] for r in o['rows']],catalogs=catalogs,db=db)
                evidence['default_catalog_revisions']=defaults
                evidence['catalog_sources']=admission_sources
                current=current_catalogs.get(wh,{})
                if current and current['revision']!=b['catalog']['revision']:
                    for sku, entry in evidence['items'].items():
                        live=current['items'].get(sku)
                        issue=allocation.status_issue(live) if live is not None else ''
                        if issue:entry['reasons'].append('当前仓资料：'+issue)
                o['rows']=[{**r,'_admission_error':'；'.join(evidence['items'][r['sku']]['reasons'])} for r in o['rows']]
            wanted = {r['sku'] for r in o['rows'] if Decimal(r['base_quantity'])>0}
            overlap = {key:{s:v for s,v in values.items() if s in wanted} for key,values in cross[wh].items()}
            if overlap['uncertain']:
                for row in o['rows']:
                    if row['sku'] in overlap['uncertain']:
                        row['_admission_error'] = '旧执行是否已反映在仓可用数中待核 '+', '.join(overlap['uncertain'][row['sku']])
            rows = allocation.allocate(o['rows'], b['catalog']['items'], balances[wh], material=material)
            value = {'gid': uuid.uuid4().hex, 'operation': runid+':'+str(i), 'rule': allocation.MATERIAL_RULE if material else allocation.RULE,
                     'head': o['head'], 'basis': o['basis'], 'bid': b['bid'], 'old': o['old'],
                     'rows': rows, 'summary': allocation.totals(rows), 'saved': wr.now(), 'position': i+1,
                     'catalog': warehouse_catalog.metadata(b['catalog']), 'draft_status': '待核对付款单'}
            if batch:
                value.update(batch_id=batch['batch_id'],batch_epoch=batch['epoch'])
                if not batch.get('new'):
                    member=next((m for m in batch['members'] if (m['head']['oid'],m['head']['warehouse'])==(o['head']['oid'],wh)),None)
                    if not member or member['status']=='cancelled':raise wr.Conflict('订单不在本批有效范围')
                    value['position']=member['position']
            if o['signed_products'] is not None:value['signed_products']=o['signed_products']
            if b.get('reserve_other_orders'): value['reserve_other_orders'] = True
            if any(overlap.values()):
                value['material_cross'] = overlap
                value['material_cross_exclude'] = sorted(x for x in exclude if x)
            if o['previous_payment'] is not None:value['previous_payment']=o['previous_payment']
            if evidence is not None:value['admission']=evidence
            plans.append(value); progress(RowProgress(f'预览分配 {i+1}/{len(orders)}：'+o['head']['number'],i+1,len(orders)))
    result = {'runid': runid, 'states': states, 'plans': plans, 'skipped': skipped, 'correction': correction, 'reserved': reserved}
    if batch:result.update(batch_id=batch['batch_id'],batch_epoch=batch['epoch'])
    return {'payload': result, 'digest': digest(result)}


def validate_plan(db, p, catalog_revisions=None):
    h = p['head']
    if p.get('batch_id'):
        import jst_batches
        jst_batches.validate_member(db,p)
    if p.get('admission'):
        from admission import snapshot
        if snapshot('',h['store'],db=db)!=p['admission']['context']:raise wr.Conflict('门店准入已变化，请重新分配：'+h['number'])
        folder=Path(db.execute('PRAGMA database_list').fetchone()[2]).parent
        latest=catalog_revisions if catalog_revisions is not None else {w:c['revision'] for w,c in warehouse_catalog.Repository(folder).catalogs().items()}
        if latest!=p['admission']['default_catalog_revisions']:raise wr.Conflict('仓资料标签依据已更新，请重新分配：'+h['number'])
    purchases.validate_head(db, h)
    blocker,previous_payment=allocation_gate(db,h,p['old'],True)
    if blocker:raise wr.Conflict(blocker+'：'+h['number'])
    if previous_payment!=p.get('previous_payment'):
        raise wr.Conflict('付款依据已变化，请重新预览分配：'+h['number'])
    if active(db,h) != p['old']:
        raise wr.Conflict('生成记录已变化：'+h['number'])
    if increases:=(p.get('manual_correction') or {}).get('material_increase'):
        from allocation_amendments import validate_material_claims
        validate_material_claims(db,h,p['old'],increases)
    if p.get('reserve_other_orders') or material_stock.shared_ledger(db):
        base = decode(db.execute('SELECT payload,digest FROM jst_baselines WHERE bid=?',(p['bid'],)).fetchone())
        seen = material_stock.cross_reservations(db,h['warehouse'],base,{r['sku'] for r in p['rows']},
            material=material_stock.is_material(db,h['oid']),batch_id=p.get('batch_id'),
            exclude=p.get('material_cross_exclude') or (p['old'],))
        if seen != p.get('material_cross', {'reserved':{}, 'uncertain':{}}):
            raise wr.Conflict('共享仓库存占用已变化，请重新预览：'+h['number'])
    # Modern returns carry all effective rows. Legacy needs the frozen upstream products.
    h = {**h, 'products':p.get('signed_products',p['basis']['products'])}
    if lc.upstream(db,h,'payment') != p['basis']:
        raise wr.Conflict('回传依据已变化：'+h['number'])


def put_generation(db, p):
    h=p['head']; raw=wr.dumps(p)
    db.execute('INSERT INTO jst_generations VALUES(?,?,?,?,?,?,?,?,?)', (p['gid'],p['operation'],h['oid'],h['revision'],h['warehouse'],h['day'],p['saved'],raw,digest(p)))
    db.execute('INSERT INTO jst_active VALUES(?,?,?) ON CONFLICT(oid,warehouse) DO UPDATE SET gid=excluded.gid', (h['oid'],h['warehouse'],p['gid']))
    stage=p['basis']['stage']
    db.execute('INSERT INTO jst_headers VALUES(?,?)',(p['gid'],wr.dumps({'summary':p['summary'],'return':p['basis']['dependencies'].get(stage),'demand_stage':stage,'revision':h['revision'],'saved':p['saved']})))
    db.executemany('INSERT INTO jst_reservations VALUES(?,?,?)', [(p['gid'],r['sku'],r['quantity']) for r in p['rows'] if Decimal(r['quantity'])>0])
    event(db,h,'生成聚水潭订单',{'gid':p['gid'],'summary':p['summary'],'old':p['old']})


def commit(path, plan, accept_issues=False, progress=lambda _: None, cancel=None):
    import jst_batches
    checkpoint(cancel); v=plan['payload']
    if digest(v) != plan['digest']:raise ValueError('分配预览被修改，请重新预览')
    if any(p['summary']['issues'] for p in v['plans']) and not accept_issues:raise ValueError('请明确核对并排除待核商品后生成')
    storage_chunks.backup_before_write(Path(path),progress); checkpoint(cancel)
    result={'generated':[], 'skipped':v['skipped'], 'zero':[], 'cancelled':False, 'errors':[]}; expected={w:tuple(s) for w,s in v['states'].items()}
    groups=[v['plans']] if v['correction'] else [[p] for p in v['plans']]
    # Canonical run facts are independent of each order's transaction. Reuse
    # the exact text while the latest deletion scope is identical, retaining
    # only one encoding (not an unbounded cache of large batch snapshots).
    clean={**v,'plans':list(v['plans']),'skipped':list(v.get('skipped',[]))}
    run_raw=wr.dumps(clean);run_digest=hashlib.sha256(run_raw.encode()).hexdigest()
    run_oids={p['head']['oid'] for p in v['plans']}|{p.get('head',{}).get('oid') for p in v.get('skipped',[])}
    run_removed=frozenset()
    blocked=set()
    catalog_check=warehouse_catalog.CatalogRevisionCheck(Path(path).parent)
    for group in groups:
        if not group:continue
        whs={p['head']['warehouse'] for p in group}
        if whs & blocked:
            result['errors'].append({'orders':[p['head']['number'] for p in group],'error':'前序提交失败，该仓剩余任务未提交'});continue
        try:
            checkpoint(cancel)
            # Catalog locks precede the DB lock, matching warehouse maintenance.
            # Parse/validate at most once, before opening the write transaction.
            with catalog_check.guard(any(p.get('admission') for p in group),cancel) as catalog_revisions, closing(sqlite3.connect(path,timeout=.1)) as db,db:
                lc.begin(db,cancel);schema(db)
                from deletion_barrier import has
                removed={r[0] for r in db.execute('SELECT oid FROM deleted_order_tokens')} if has(db,'deleted_order_tokens') else set()
                from deletion_barrier import active as source_active
                import warehouse_execution
                def removed_scope(plan):
                    h=plan['head'];return h['oid'] in removed or not source_active(db,h['oid'],h['warehouse']) or not warehouse_execution.is_active(db,h['oid'],h['warehouse'])
                # Replaying a frozen shared run must account for its own earlier
                # committed steps even when those source orders were deleted.
                # Never adopt another run's inventory serial or new baseline.
                advanced=set()
                if has(db,'deleted_jst_steps'):
                    for p in group:
                        w=p['head']['warehouse']
                        if removed_scope(p) and db.execute('SELECT 1 FROM deleted_jst_steps WHERE operation=? AND runid=? AND warehouse=? AND bid=?',(p['operation'],v['runid'],w,expected[w][0])).fetchone():advanced.add(w)
                for w in advanced:expected[w]=(expected[w][0],expected[w][1]+1)
                group=[p for p in group if not removed_scope(p)]
                if not group:continue
                whs={p['head']['warehouse'] for p in group}
                existing=[db.execute('SELECT result FROM jst_steps WHERE operation=?',(p['operation'],)).fetchone() for p in group]
                if all(existing):
                    if v.get('batch_id'):
                        for p in group:jst_batches.validate_member(db,p,replay=True)
                    for r in existing:
                        saved=wr.loads(r[0]);result['generated' if saved['gid'] else 'zero'].append(saved['gid'] or saved['number'])
                    for w in whs:expected[w]=(expected[w][0],expected[w][1]+1)
                    continue
                if any(existing):raise wr.Conflict('更正操作记录不完整，请核对')
                for w in whs:
                    actual=jst_batches.state(db,v['batch_id'],w) if v.get('batch_id') else state(db,w)
                    if actual!=expected[w]:raise wr.Conflict(w+'库存基准或预留已变化，请重新预览')
                for p in group:checkpoint(cancel);validate_plan(db,p,catalog_revisions)
                for p in group:
                    if p['summary']['positive']:
                        put_generation(db,p)
                    else:
                        if v['correction'] and p['old']:db.execute('DELETE FROM jst_active WHERE oid=? AND warehouse=?',(p['head']['oid'],p['head']['warehouse']))
                        event(db,p['head'],'手动确认为零分配' if p.get('manual_correction') else '库存不足未生成',{'rows':p['rows'],'runid':v['runid'],'batch_id':p.get('batch_id')})
                    if audit:=p.get('manual_correction'):
                        event(db,p['head'],'手动更正分配',{**audit,'gid':p['gid'] if p['summary']['positive'] else None})
                    db.execute('INSERT INTO jst_steps VALUES(?,?,?)',(p['operation'],v['runid'],wr.dumps({'gid':p['gid'] if p['summary']['positive'] else None,'number':p['head']['number']})))
                    jst_batches.record(db,p)
                for w in whs:
                    if v.get('batch_id'):db.execute('UPDATE jst_batch_stock SET serial=serial+1 WHERE batch_id=? AND warehouse=?',(v['batch_id'],w))
                    else:db.execute('UPDATE jst_warehouses SET serial=serial+1 WHERE warehouse=?',(w,))
                scope=frozenset(removed & run_oids)
                if scope!=run_removed:
                    clean={**v,'plans':[p for p in v['plans'] if p['head']['oid'] not in removed],'skipped':[p for p in v.get('skipped',[]) if p.get('head',{}).get('oid') not in removed]}
                    run_raw=wr.dumps(clean);run_digest=hashlib.sha256(run_raw.encode()).hexdigest();run_removed=scope
                db.execute('INSERT OR IGNORE INTO jst_runs VALUES(?,?,?,?)',(v['runid'],wr.now(),run_raw,run_digest))
                if catalog_revisions is not None:catalog_check.verify()
                checkpoint(cancel);progress(CommitProgress(True));db.commit()
                # Advance only our own transaction; never adopt a concurrent refresh after COMMIT.
                for w in whs:expected[w]=(expected[w][0],expected[w][1]+1)
            progress(CommitProgress(False))
            for p in group:
                (result['generated'] if p['summary']['positive'] else result['zero']).append(p['gid'] if p['summary']['positive'] else p['head']['number'])
                notice=RowProgress('已保存可导出：'+p['head']['number'],len(result['generated'])+len(result['zero']),len(v['plans']))
                notice.business_event={'gid':p['gid'],'number':p['head']['number'],'positive':bool(p['summary']['positive'])}
                progress(notice)
        except Cancelled:
            result['cancelled']=True;break
        except Exception as exc:
            blocked.update(whs);result['errors'].append({'orders':[p['head']['number'] for p in group],'error':str(exc)})
        finally:
            progress(CommitProgress(False))
    if result['cancelled'] or result['errors']:
        # Audit only: cancellation never fabricates an order. Do not wait on someone else's write lock.
        try:
            with closing(sqlite3.connect(path,timeout=.1)) as db,db:
                db.execute('BEGIN IMMEDIATE')
                from deletion_barrier import active
                for h in [p['head'] for p in v['plans']]:
                    if not active(db,h['oid'],h.get('warehouse')):continue
                    event(db,h,'批次停止' if result['cancelled'] else '批次存在未完成订单',{'runid':v['runid'],'result':result})
        except sqlite3.Error as exc:
            import logging
            result['audit_warning']='数据库忙，未能保存停止／失败日志：'+str(exc)
            logging.exception(result['audit_warning'])
    return result


class _LockCommitBusy(Exception):
    def __init__(self,error):self.error=error


class _LockRollbackWait(str):
    active=False


_LOCK_BUSY_SECONDS=10


def lock(path, gids, reason='', unlock=False, progress=lambda _:None, cancel=None):
    orders=[get(path,g,cancel=cancel) for g in gids]
    if unlock and not reason.strip():raise ValueError('更正已锁订单必须记录外部撤销／更正情况及原因')
    storage_chunks.backup_before_write(Path(path),progress);checkpoint(cancel)
    deadline=time.monotonic()+_LOCK_BUSY_SECONDS
    while True:
        try:
            _lock_once(path,orders,reason,unlock,progress,cancel,deadline)
            progress(CommitProgress(False));return gids
        except _LockCommitBusy as blocked:
            if time.monotonic()>=deadline:raise blocked.error
            checkpoint(cancel)
            progress(_LockRollbackWait('本地读取短暂占用，锁单尝试已安全回滚；正在重新核验后保存，不会重复锁单'))
            time.sleep(.025)


def _lock_once(path,orders,reason,unlock,progress,cancel,deadline):
    with closing(sqlite3.connect(path,timeout=.1)) as db,db:
        lc.begin(db,cancel,deadline=deadline);schema(db)
        for p in orders:
            checkpoint(cancel);h=p['head'];purchases.validate_head(db,h)
            if active(db,h)!=p['gid']:raise wr.Conflict('生成单已变化，请重新核对')
            if unlock:
                from jst_auto_order_store import RELEASED,state as automatic_state
                auto=automatic_state(db,h)
                if auto and auto[0] in RELEASED:
                    raise ValueError('此单已有自动下单记录，请继续核对原平台订单')
            if unlock and (lc.current(db,h,'payment') or lc.current(db,h,'receipt')):raise ValueError('已有实际付款／收货，不能解除保护覆盖下游事实')
            if not unlock:
                if lc.upstream(db,{**h,'products':p.get('signed_products',p['basis']['products'])},'payment')!=p['basis']:raise wr.Conflict('回传已变化，不能锁定旧生成单')
            if bool(locked(db,h)) == (not unlock):continue
            db.execute('INSERT INTO jst_locks VALUES(?,?,?,?,?,?) ON CONFLICT(oid,warehouse) DO UPDATE SET gid=excluded.gid,locked=excluded.locked,at=excluded.at,reason=excluded.reason',(h['oid'],h['warehouse'],p['gid'],int(not unlock),wr.now(),reason))
            event(db,h,'更正解除锁定' if unlock else '确认下单并锁定',{'gid':p['gid'],'reason':reason})
        checkpoint(cancel);progress(CommitProgress(True))
        try:db.commit()
        except sqlite3.OperationalError as exc:
            code=getattr(exc,'sqlite_errorcode',None)
            busy=(code & 255)==sqlite3.SQLITE_BUSY if isinstance(code,int) else str(exc).lower()=='database is locked'
            if not busy or not db.in_transaction:raise
            db.rollback()  # A failed/unknown rollback must never be replayed.
            raise _LockCommitBusy(exc) from exc


def payment_plan(path, gid, progress=lambda _:None, cancel=None):
    p=get(path,gid,cancel=cancel);h=lc.full_head(path,p['head'])
    with closing(sqlite3.connect(path)) as db:
        if active(db,h)!=gid:raise wr.Conflict('请核对当前有效生成单')
        proofs={}
        if lc.upstream(db,h,'payment',_proofs=proofs)!=p['basis']:raise wr.Conflict('回传已变化，请先核对生成依据')
        rows=[]
        for r in p['rows']:
            if Decimal(r['quantity'])<=0:continue
            rows.append({'sku':r['sku'],'name':r['name'],'quantity':r['quantity'],'base_quantity':r['quantity'],'unit':'库存单位','factor':None,'conversion_basis':None,'amount':r['amount'],'price':r['price'],'currency':'CNY','document_number':None,'document_date':None,'note':None,'sources':[],'raw_headers':[],'raw_values':[],'adapter':'jst_import_return','amount_basis':'数量×单价','external_order_number':None})
        from purchase_io import number
        for row in rows:
            for field in ('quantity','base_quantity','amount','price'):row[field]=number(row[field])
            row['document_number']=''
        plan=lc.make_plan(db,h,'payment',rows,[],_verified=proofs)
        plan['payload']['jst_generation']=gid;plan['digest']=lc.fingerprint(plan['payload'])
        old=lc.current(db,h,'payment',_verified=plan.get('_read_proofs'));plan['changed']=not old or old['digest']!=plan['digest']
    return {'plans':[plan],'files':[], 'jst_generation':gid}


def cancel_generated(path,gids,reason,progress=lambda _:None,cancel=None):
    if not reason.strip():raise ValueError('撤销生成单须填写原因')
    orders=[get(path,g,cancel=cancel) for g in gids]
    storage_chunks.backup_before_write(Path(path),progress);checkpoint(cancel)
    with closing(sqlite3.connect(path,timeout=.1)) as db,db:
        lc.begin(db,cancel)
        for p in orders:
            checkpoint(cancel);h=p['head'];purchases.validate_head(db,h)
            if active(db,h)!=p['gid']:raise wr.Conflict('生成单已变化，请重新核对')
            guard=saved_execution_reason(db,h)
            if guard or lc.current(db,h,'payment') or lc.current(db,h,'receipt'):
                raise ValueError((guard or '已有付款或收货资料')+'，不可作为未下单生成单撤销')
            from special_plans import exists as has_special
            if has_special(db,'special_packages') and db.execute('SELECT 1 FROM special_packages WHERE oid=?',(h['oid'],)).fetchone():
                from special_settlement import release_unsubmitted
                release_unsubmitted(db,h['oid'],'撤销生成单',reason)
        for p in orders:
            release_generation(db,p['head'],p['gid'],p.get('batch_id'),reason)
        checkpoint(cancel);progress(CommitProgress(True));db.commit()
    progress(CommitProgress(False));return gids


def release_generation(db, head, gid, batch_id, reason, **evidence):
    """Retire one confirmed live reference in the caller's write transaction."""
    import jst_batches

    if db.execute('DELETE FROM jst_active WHERE oid=? AND warehouse=? AND gid=?',
                  (head['oid'],head['warehouse'],gid)).rowcount != 1:
        raise wr.Conflict('原生成单已变化，请重新核对')
    jst_batches.cancel_member(db,head,gid)
    if not batch_id:
        db.execute('UPDATE jst_warehouses SET serial=serial+1 WHERE warehouse=?',(head['warehouse'],))
    event(db,head,'撤销未下单生成单',{'gid':gid,'reason':reason,**evidence})


def export_source(path,gid,target,progress=lambda _:None,cancel=None):
    import source_archive,os,tempfile
    p=get(path,gid,cancel=cancel);sha=p['catalog'].get('source_sha256')
    with closing(sqlite3.connect(path)) as db:
        row=db.execute('SELECT name,content FROM jst_files WHERE sha=?',(sha,)).fetchone() or source_archive.read(db,sha)
    if not row:raise ValueError('当时原件未留存，不能使用当前资料替代')
    if hashlib.sha256(row[1]).hexdigest()!=sha:raise ValueError('原件校验失败')
    target=Path(target);fd,tmp=tempfile.mkstemp(dir=target.parent,prefix='.jst-source-')
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(row[1])
        checkpoint(cancel);os.link(tmp,target)
    finally:Path(tmp).unlink(missing_ok=True)
    return str(target)
