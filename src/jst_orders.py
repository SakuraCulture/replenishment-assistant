"""Local warehouse baselines, immutable execution orders and atomic reservations.

All new tables are additive. Signed/returned/model quantities are never rewritten.
New warehouse stock already includes orders locked BEFORE that refresh.
"""
import copy
import hashlib
import sqlite3
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
from runtime_support import checkpoint, CommitProgress, Cancelled
from export_progress import RowProgress


def digest(value):
    return hashlib.sha256(wr.dumps(value).encode()).hexdigest()


def exists(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='jst_generations'").fetchone())


def schema(db):
    for sql in (
        'CREATE TABLE IF NOT EXISTS jst_baselines(bid TEXT PRIMARY KEY,warehouse TEXT NOT NULL,created TEXT NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS jst_warehouses(warehouse TEXT PRIMARY KEY,bid TEXT NOT NULL,serial INTEGER NOT NULL)',
        'CREATE TABLE IF NOT EXISTS jst_generations(gid TEXT PRIMARY KEY,operation TEXT UNIQUE NOT NULL,oid TEXT NOT NULL,revision INTEGER NOT NULL,warehouse TEXT NOT NULL,day TEXT NOT NULL,saved TEXT NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS jst_active(oid TEXT,warehouse TEXT,gid TEXT NOT NULL,PRIMARY KEY(oid,warehouse))',
        'CREATE TABLE IF NOT EXISTS jst_headers(gid TEXT PRIMARY KEY,payload TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS jst_locks(oid TEXT,warehouse TEXT,gid TEXT NOT NULL,locked INTEGER NOT NULL,at TEXT NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(oid,warehouse))',
        'CREATE TABLE IF NOT EXISTS jst_reservations(gid TEXT,sku TEXT,quantity TEXT NOT NULL,PRIMARY KEY(gid,sku))',
        'CREATE TABLE IF NOT EXISTS jst_events(id INTEGER PRIMARY KEY,at TEXT,day TEXT,oid TEXT,warehouse TEXT,action TEXT,detail TEXT)',
        'CREATE TABLE IF NOT EXISTS jst_files(sha TEXT PRIMARY KEY,name TEXT,content BLOB)',
        'CREATE TABLE IF NOT EXISTS jst_runs(runid TEXT PRIMARY KEY,created TEXT,payload TEXT,digest TEXT)',
        'CREATE TABLE IF NOT EXISTS jst_steps(operation TEXT PRIMARY KEY,runid TEXT NOT NULL,result TEXT NOT NULL)',
        'CREATE INDEX IF NOT EXISTS jst_event_day ON jst_events(day,id)',
        'CREATE INDEX IF NOT EXISTS jst_generation_order ON jst_generations(oid,warehouse)',
    ):
        db.execute(sql)


def state(db, warehouse):
    if not exists(db):
        return None
    return db.execute('SELECT bid,serial FROM jst_warehouses WHERE warehouse=?', (warehouse,)).fetchone()


def active(db, head):
    if not exists(db):
        return None
    row = db.execute('SELECT gid FROM jst_active WHERE oid=? AND warehouse=?', (head['oid'], head['warehouse'])).fetchone()
    return row[0] if row else None


def locked(db, head):
    if not exists(db):
        return None
    row = db.execute('SELECT gid FROM jst_locks WHERE oid=? AND warehouse=? AND locked=1', (head['oid'], head['warehouse'])).fetchone()
    return row[0] if row else None


def guard_resign(db, oid):
    if exists(db) and db.execute('SELECT 1 FROM jst_active WHERE oid=?', (oid,)).fetchone():
        raise wr.Conflict('本单已有聚水潭生成记录；请先在订单跟踪核对下游，不能覆盖签发绕过库存占用或锁单')


def guard_return(db, head):
    if locked(db, head):
        raise wr.Conflict('该订单已下单锁定；请先通过更正已锁订单核对外部执行，不能直接覆盖回传')


def event(db, head, action, detail):
    db.execute('INSERT INTO jst_events(at,day,oid,warehouse,action,detail) VALUES(?,?,?,?,?,?)',
               (wr.now(), head.get('day', wr.today()), head.get('oid'), head.get('warehouse'), action, wr.dumps(detail)))


def decode(row):
    if not row:
        return None
    value = wr.loads(row[0])
    if digest(value) != row[1]:
        raise ValueError('聚水潭快照校验失败')
    return value


def get(path, gid, progress=lambda _: None, cancel=None):
    checkpoint(cancel)
    with closing(sqlite3.connect(path)) as db:
        if not exists(db):
            raise ValueError('没有已保存生成单')
        value = decode(db.execute('SELECT payload,digest FROM jst_generations WHERE gid=?', (gid,)).fetchone())
        if value is None:
            raise ValueError('生成单不存在')
        if value.get('gid') != gid:
            raise ValueError('生成记录标识与快照不一致')
        return value


def baseline(db, warehouse):
    current = state(db, warehouse)
    if current is None:
        return None
    return decode(db.execute('SELECT payload,digest FROM jst_baselines WHERE bid=?', (current[0],)).fetchone())


def catalogs(path, folder=None, progress=lambda _: None, cancel=None):
    """Shared current stock wins over a stale default. This is a read-only query."""
    defaults = warehouse_catalog.Repository(folder).catalogs() if folder else {}
    out = {}
    with closing(sqlite3.connect(path)) as db:
        for wh in warehouse_catalog.WAREHOUSES:
            checkpoint(cancel); b = baseline(db, wh)
            if b:
                out[wh] = {'bid': b['bid'], 'catalog': b['catalog'], 'current': True}
            elif wh in defaults:
                out[wh] = {'bid': None, 'catalog': defaults[wh], 'current': False}
    return out


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
    if not basis['ready']:
        raise ValueError('门店回传尚未确认或依据已变化：' + head['number'])
    doc = lc.current(db, head, 'return')
    rows = doc['payload']['rows'] if doc else [dict(sku=s, name=p.get('name'), base_quantity=p['quantity'], sources=[]) for s,p in basis['products'].items()]
    legacy = lc.legacy_return(db,head,head['products']) if doc is None else None
    return basis, rows, doc['saved'] if doc else legacy['saved'], copy.deepcopy(head['products']) if doc is None else None


def reservations(db, wh, included, exclude=()):
    values = {}
    for gid, sku, qty in db.execute('SELECT a.gid,r.sku,r.quantity FROM jst_active a JOIN jst_reservations r ON a.gid=r.gid WHERE a.warehouse=? ORDER BY a.rowid,r.rowid', (wh,)):
        if gid in included or gid in exclude:
            continue
        values[sku] = values.get(sku, Decimal(0)) + Decimal(qty)
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='retired_stock'").fetchone():
        current=state(db,wh)
        if current:
            for sku,qty in db.execute('SELECT sku,quantity FROM retired_stock WHERE warehouse=? AND bid=?',(wh,current[0])):
                values[sku]=values.get(sku,Decimal(0))+Decimal(qty)
    return values


def prepare(path, heads, manual_order=False, correction=False, progress=lambda _: None, cancel=None):
    runid = uuid.uuid4().hex; orders = []; skipped = []; states = {}; bases = {}; seen = set()
    with closing(sqlite3.connect(path)) as db:
        db.execute('BEGIN')
        for supplied in heads:
            checkpoint(cancel); h = lc.full_head(path, supplied); key = (h['oid'], h['warehouse'])
            if key in seen:
                raise ValueError('重复选择同一店仓订单')
            seen.add(key); lock = locked(db, h); old = active(db, h)
            if lock or lc.current(db,h,'payment') or lc.current(db,h,'receipt') or old and not correction:
                skipped.append({'head': supplied, 'reason': '已锁单' if lock else '已有付款／收货' if lc.current(db,h,'payment') or lc.current(db,h,'receipt') else '已生成，原样导出或选择重新分配'}); continue
            basis, rows, saved, signed_products = return_basis(db, h)
            if not any(Decimal(r['base_quantity']) > 0 for r in rows):
                skipped.append({'head': supplied, 'reason': '无要货，无需生成'}); continue
            wh = h['warehouse']
            if wh not in bases:bases[wh]=baseline(db,wh)
            b=bases[wh]
            if b is None:
                raise ValueError(wh + '尚未建立库存基准，请导入或采用默认仓资料')
            states[wh] = state(db, wh)
            compact = {k:v for k,v in h.items() if k != 'products'}
            orders.append({'head': compact, 'basis': basis, 'rows': rows, 'saved': saved, 'old': old, 'signed_products':signed_products})
        if correction:
            # A correction changes subsequent balances; include ALL unlocked active orders in each affected warehouse.
            chosen = {o['old'] for o in orders if o['old']}
            for wh in states:
                outstanding = {g for o,w,g in db.execute('SELECT oid,warehouse,gid FROM jst_active WHERE warehouse=?', (wh,)) if not locked(db, {'oid':o,'warehouse':w})}
                if not outstanding <= chosen:
                    raise ValueError('重新分配须选齐该仓全部未锁已生成订单，避免改变其它订单占用')
        if not manual_order:
            orders.sort(key=lambda o: (o['saved'], o['head']['day'], o['head']['number']))
        balances = {}; reserved = {}
        for wh, b in bases.items():
            used = reservations(db, wh, set(b['included']), {o['old'] for o in orders if correction and o['old']})
            reserved[wh] = {s:allocation.text(q) for s,q in used.items()}; balances[wh] = {}
            for sku, item in b['catalog']['items'].items():
                try: balances[wh][sku] = allocation.number(item.get('可用数'), '可用库存') - used.get(sku, Decimal(0))
                except ValueError: pass
        plans = []
        for i, o in enumerate(orders):
            checkpoint(cancel); wh=o['head']['warehouse']; b=bases[wh]
            rows = allocation.allocate(o['rows'], b['catalog']['items'], balances[wh])
            value = {'gid': uuid.uuid4().hex, 'operation': runid+':'+str(i), 'rule': allocation.RULE,
                     'head': o['head'], 'basis': o['basis'], 'bid': b['bid'], 'old': o['old'],
                     'rows': rows, 'summary': allocation.totals(rows), 'saved': wr.now(), 'position': i+1,
                     'catalog': warehouse_catalog.metadata(b['catalog']), 'draft_status': '待核对付款单'}
            if o['signed_products'] is not None:value['signed_products']=o['signed_products']
            plans.append(value); progress(RowProgress(f'预览分配 {i+1}/{len(orders)}：'+o['head']['number'],i+1,len(orders)))
    result = {'runid': runid, 'states': states, 'plans': plans, 'skipped': skipped, 'correction': correction, 'reserved': reserved}
    return {'payload': result, 'digest': digest(result)}


def validate_plan(db, p):
    h = p['head']
    purchases.validate_head(db, h)
    if locked(db,h) or lc.current(db,h,'payment') or lc.current(db,h,'receipt'):
        raise wr.Conflict('订单已锁或已有付款／收货，请重新核对：'+h['number'])
    if active(db,h) != p['old']:
        raise wr.Conflict('生成记录已变化：'+h['number'])
    # Modern returns carry all effective rows. Legacy needs the frozen upstream products.
    h = {**h, 'products':p.get('signed_products',p['basis']['products'])}
    if lc.upstream(db,h,'payment') != p['basis']:
        raise wr.Conflict('回传依据已变化：'+h['number'])


def put_generation(db, p):
    h=p['head']; raw=wr.dumps(p)
    db.execute('INSERT INTO jst_generations VALUES(?,?,?,?,?,?,?,?,?)', (p['gid'],p['operation'],h['oid'],h['revision'],h['warehouse'],h['day'],p['saved'],raw,digest(p)))
    db.execute('INSERT INTO jst_active VALUES(?,?,?) ON CONFLICT(oid,warehouse) DO UPDATE SET gid=excluded.gid', (h['oid'],h['warehouse'],p['gid']))
    db.execute('INSERT INTO jst_headers VALUES(?,?)',(p['gid'],wr.dumps({'summary':p['summary'],'return':p['basis']['dependencies'].get('return'),'revision':h['revision'],'saved':p['saved']})))
    db.executemany('INSERT INTO jst_reservations VALUES(?,?,?)', [(p['gid'],r['sku'],r['quantity']) for r in p['rows'] if Decimal(r['quantity'])>0])
    event(db,h,'生成聚水潭订单',{'gid':p['gid'],'summary':p['summary'],'old':p['old']})


def commit(path, plan, accept_issues=False, progress=lambda _: None, cancel=None):
    checkpoint(cancel); v=plan['payload']
    if digest(v) != plan['digest']:raise ValueError('分配预览被修改，请重新预览')
    if any(p['summary']['issues'] for p in v['plans']) and not accept_issues:raise ValueError('请明确核对并排除待核商品后生成')
    storage_chunks.backup_before_write(Path(path),progress); checkpoint(cancel)
    result={'generated':[], 'skipped':v['skipped'], 'zero':[], 'cancelled':False, 'errors':[]}; expected={w:tuple(s) for w,s in v['states'].items()}
    groups=[v['plans']] if v['correction'] else [[p] for p in v['plans']]
    run_raw=wr.dumps(v)
    blocked=set()
    for group in groups:
        if not group:continue
        whs={p['head']['warehouse'] for p in group}
        if whs & blocked:
            result['errors'].append({'orders':[p['head']['number'] for p in group],'error':'前序提交失败，该仓剩余任务未提交'});continue
        try:
            checkpoint(cancel)
            with closing(sqlite3.connect(path,timeout=.1)) as db,db:
                lc.begin(db,cancel);schema(db)
                existing=[db.execute('SELECT result FROM jst_steps WHERE operation=?',(p['operation'],)).fetchone() for p in group]
                if all(existing):
                    for r in existing:
                        saved=wr.loads(r[0]);result['generated' if saved['gid'] else 'zero'].append(saved['gid'] or saved['number'])
                    for w in whs:expected[w]=(expected[w][0],expected[w][1]+1)
                    continue
                if any(existing):raise wr.Conflict('更正操作记录不完整，请核对')
                for w in whs:
                    if state(db,w)!=expected[w]:raise wr.Conflict(w+'库存基准或预留已变化，请重新预览')
                for p in group:checkpoint(cancel);validate_plan(db,p)
                for p in group:
                    if p['summary']['positive']:
                        put_generation(db,p)
                    else:
                        if v['correction'] and p['old']:db.execute('DELETE FROM jst_active WHERE oid=? AND warehouse=?',(p['head']['oid'],p['head']['warehouse']))
                        event(db,p['head'],'库存不足未生成',{'rows':p['rows'],'runid':v['runid']})
                    db.execute('INSERT INTO jst_steps VALUES(?,?,?)',(p['operation'],v['runid'],wr.dumps({'gid':p['gid'] if p['summary']['positive'] else None,'number':p['head']['number']})))
                for w in whs:db.execute('UPDATE jst_warehouses SET serial=serial+1 WHERE warehouse=?',(w,))
                db.execute('INSERT OR IGNORE INTO jst_runs VALUES(?,?,?,?)',(v['runid'],wr.now(),run_raw,plan['digest']))
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
                for h in [p['head'] for p in v['plans']]:
                    event(db,h,'批次停止' if result['cancelled'] else '批次存在未完成订单',{'runid':v['runid'],'result':result})
        except sqlite3.Error as exc:
            import logging
            result['audit_warning']='数据库忙，未能保存停止／失败日志：'+str(exc)
            logging.exception(result['audit_warning'])
    return result


def lock(path, gids, reason='', unlock=False, progress=lambda _:None, cancel=None):
    orders=[get(path,g,cancel=cancel) for g in gids]
    if unlock and not reason.strip():raise ValueError('更正已锁订单必须记录外部撤销／更正情况及原因')
    storage_chunks.backup_before_write(Path(path),progress);checkpoint(cancel)
    with closing(sqlite3.connect(path,timeout=.1)) as db,db:
        lc.begin(db,cancel);schema(db)
        for p in orders:
            checkpoint(cancel);h=p['head'];purchases.validate_head(db,h)
            if active(db,h)!=p['gid']:raise wr.Conflict('生成单已变化，请重新核对')
            if unlock and (lc.current(db,h,'payment') or lc.current(db,h,'receipt')):raise ValueError('已有实际付款／收货，不能解除保护覆盖下游事实')
            if not unlock:
                if lc.upstream(db,{**h,'products':p.get('signed_products',p['basis']['products'])},'payment')!=p['basis']:raise wr.Conflict('回传已变化，不能锁定旧生成单')
            if bool(locked(db,h)) == (not unlock):continue
            db.execute('INSERT INTO jst_locks VALUES(?,?,?,?,?,?) ON CONFLICT(oid,warehouse) DO UPDATE SET gid=excluded.gid,locked=excluded.locked,at=excluded.at,reason=excluded.reason',(h['oid'],h['warehouse'],p['gid'],int(not unlock),wr.now(),reason))
            event(db,h,'更正解除锁定' if unlock else '确认下单并锁定',{'gid':p['gid'],'reason':reason})
        checkpoint(cancel);progress(CommitProgress(True));db.commit()
    progress(CommitProgress(False));return gids


def payment_plan(path, gid, progress=lambda _:None, cancel=None):
    p=get(path,gid,cancel=cancel);h=lc.full_head(path,p['head'])
    with closing(sqlite3.connect(path)) as db:
        if active(db,h)!=gid:raise wr.Conflict('请核对当前有效生成单')
        if lc.upstream(db,h,'payment')!=p['basis']:raise wr.Conflict('回传已变化，请先核对生成依据')
        rows=[]
        for r in p['rows']:
            if Decimal(r['quantity'])<=0:continue
            rows.append({'sku':r['sku'],'name':r['name'],'quantity':r['quantity'],'base_quantity':r['quantity'],'unit':'库存单位','factor':None,'conversion_basis':None,'amount':r['amount'],'price':r['price'],'currency':'CNY','document_number':None,'document_date':None,'note':None,'sources':[],'raw_headers':[],'raw_values':[],'adapter':'jst_import_return','amount_basis':'数量×单价','external_order_number':None})
        from purchase_io import number
        for row in rows:
            for field in ('quantity','base_quantity','amount','price'):row[field]=number(row[field])
            row['document_number']=''
        plan=lc.make_plan(db,h,'payment',rows,[])
        plan['payload']['jst_generation']=gid;plan['digest']=lc.fingerprint(plan['payload'])
        old=lc.current(db,h,'payment');plan['changed']=not old or old['digest']!=plan['digest']
    return {'plans':[plan],'files':[], 'jst_generation':gid}


def guard_payment(db, p):
    gid=p['payload'].get('jst_generation')
    if gid and active(db,p['head'])!=gid:raise wr.Conflict('付款草稿对应生成单已变化，请重新核对')


def cancel_generated(path,gids,reason,progress=lambda _:None,cancel=None):
    if not reason.strip():raise ValueError('撤销生成单须填写原因')
    orders=[get(path,g,cancel=cancel) for g in gids]
    storage_chunks.backup_before_write(Path(path),progress);checkpoint(cancel)
    with closing(sqlite3.connect(path,timeout=.1)) as db,db:
        lc.begin(db,cancel)
        for p in orders:
            checkpoint(cancel);h=p['head'];purchases.validate_head(db,h)
            if active(db,h)!=p['gid']:raise wr.Conflict('生成单已变化，请重新核对')
            if locked(db,h) or lc.current(db,h,'payment') or lc.current(db,h,'receipt'):raise ValueError('已锁或已有付款／收货，不可作为未下单生成单撤销')
        for p in orders:
            h=p['head'];db.execute('DELETE FROM jst_active WHERE oid=? AND warehouse=?',(h['oid'],h['warehouse']))
            db.execute('UPDATE jst_warehouses SET serial=serial+1 WHERE warehouse=?',(h['warehouse'],))
            event(db,h,'撤销未下单生成单',{'gid':p['gid'],'reason':reason})
        checkpoint(cancel);progress(CommitProgress(True));db.commit()
    progress(CommitProgress(False));return gids


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
