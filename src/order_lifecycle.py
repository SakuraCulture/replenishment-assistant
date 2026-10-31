"""Whole-order downstream facts, independently versioned from immutable signing.

No computation or implicit payment conversion. Old ledgers are read-only inputs.
"""
import copy, hashlib, os, sqlite3, tempfile, time, uuid
from contextlib import closing
from decimal import Decimal
from pathlib import Path
import work_repository as wr
import purchases, merchant_feedback, normal_exports, archive_orders as ao
import lifecycle_rules as rules
from history_read import repository
from runtime_support import checkpoint, Cancelled
from export_progress import RowProgress
from lifecycle_io import STAGES, TITLES, QUANTITIES, DOCNUM, DOCDATE, fields

SCHEMA = 2
STATUSES = ('待回传', '待付款', '待收货', '已收齐')
COMMIT_BOUNDARY = '正在原子提交单据；提交完成前不可取消'

def schema(db):
    for sql in (
        'CREATE TABLE IF NOT EXISTS lifecycle_versions(oid TEXT,revision INTEGER,warehouse TEXT,stage TEXT,version INTEGER,saved TEXT,digest TEXT,payload TEXT,PRIMARY KEY(oid,revision,warehouse,stage,version))',
        'CREATE TABLE IF NOT EXISTS lifecycle_files(sha TEXT PRIMARY KEY,name TEXT,content BLOB)',
        'CREATE TABLE IF NOT EXISTS lifecycle_events(id INTEGER PRIMARY KEY,at TEXT,oid TEXT,revision INTEGER,warehouse TEXT,stage TEXT,version INTEGER,action TEXT,detail TEXT)',
        'CREATE TABLE IF NOT EXISTS lifecycle_mappings(stage TEXT,name TEXT,payload TEXT,PRIMARY KEY(stage,name))',
    ): db.execute(sql)

def exists(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE name='lifecycle_versions'").fetchone() is not None

def key(head): return (head['oid'], head['revision'], head['warehouse'])

def fingerprint(payload):
    value = copy.deepcopy(payload)
    value['files'] = [{k: f[k] for k in ('name', 'sha')} for f in value['files']]
    return hashlib.sha256(wr.dumps(value).encode()).hexdigest()

def decode(row):
    if not row: return None
    version, saved, digest, raw = row; payload = wr.loads(raw)
    if payload.get('schema') not in (1,SCHEMA) or fingerprint(payload) != digest: raise ValueError('生命周期单据快照校验失败')
    return {'version': version, 'saved': saved, 'digest': digest, 'payload': payload}

def current(db, head, stage, version=None):
    if not exists(db): return None
    condition = ' AND version=?' if version is not None else ''
    args = (*key(head), stage, *((version,) if version is not None else ()))
    return decode(db.execute('SELECT version,saved,digest,payload FROM lifecycle_versions WHERE oid=? AND revision=? AND warehouse=? AND stage=?' + condition + ' ORDER BY version DESC LIMIT 1', args).fetchone())

def history(path, head, stage, progress=lambda _: None, cancel=None):
    with closing(purchases.connect(path)) as db:
        if not exists(db): return []
        result = []
        for row in db.execute('SELECT version,saved,digest,payload FROM lifecycle_versions WHERE oid=? AND revision=? AND warehouse=? AND stage=? ORDER BY version DESC', (*key(head), stage)):
            checkpoint(cancel); result.append(decode(row))
        return result

def mappings(path, stage, name=None, value=None, progress=lambda _: None, cancel=None):
    checkpoint(cancel)
    if value is not None:
        import storage_chunks
        storage_chunks.backup_before_write(Path(path), progress)
    with closing(purchases.connect(path)) as db, db:
        if value is not None:
            checkpoint(cancel); schema(db)
            db.execute('INSERT INTO lifecycle_mappings VALUES(?,?,?) ON CONFLICT(stage,name) DO UPDATE SET payload=excluded.payload', (stage, name, wr.dumps(value)))
        if not exists(db): return {}
        return {n: wr.loads(p) for n, p in db.execute('SELECT name,payload FROM lifecycle_mappings WHERE stage=? ORDER BY name', (stage,))}

def binding_key(row):
    return wr.dumps([row.get(k) for k in ('signed_number', 'revision', 'store', 'warehouse', 'document_number')])

def matching(row, heads):
    return [h for h in heads if all(not row.get(f) or row[f] == h[k] for f, k in (('signed_number', 'number'), ('revision', 'revision'), ('store', 'store'), ('warehouse', 'warehouse')))]

def available(path, scanned, progress=lambda _: None, cancel=None):
    scopes = set(); unknown = False
    with closing(purchases.connect(path)) as db:
        for row in scanned['rows']:
            checkpoint(cancel)
            if row['signed_number']:
                identity, wh = purchases.wo.resolve(db, row['signed_number'])
                scopes.update((identity[1], w) for w in ([wh] if wh else purchases.wo.WAREHOUSES))
            elif row['store']:
                scopes.update((row['store'], w) for w in ([row['warehouse']] if row['warehouse'] else purchases.wo.WAREHOUSES))
            else: unknown = True
    return purchases.targets(path, None if unknown else scopes, progress, cancel)

def upstream(db, head, stage):
    """Freeze every dependency; absence is also a token checked at commit."""
    index=STAGES.index(stage); tokens={}; previous=None; valid=True
    for s in STAGES[:index]:
        doc=current(db,head,s)
        if doc is None and s=='return':
            legacy=legacy_return(db,head,head['products'])
            if legacy and legacy['complete']:
                doc={'version':legacy['version'],'digest':legacy['digest'],'payload':{'schema':1,'rows':[{'sku':k,'name':head['products'][k].get('name'),'base_quantity':str(v)} for k,v in legacy['values'].items()]}}
        tokens[s]=rules.basis_token(doc)
        if doc is None:
            valid=False
        elif doc['payload'].get('schema')==SCHEMA:
            p=doc['payload']
            if p.get('pending') or p.get('basis',{}).get('dependencies')!={k:v for k,v in tokens.items() if k!=s}:valid=False
        previous=doc
    products=head['products'] if stage=='return' else rules.positive(previous['payload']['rows']) if previous and valid else {}
    return {'stage':'signed' if stage=='return' else STAGES[index-1], 'dependencies':tokens, 'signed_digest':head['digest'], 'ready':stage=='return' or valid, 'products':products}

def make_plan(db, head, stage, raw_rows, files, duplicates=0, explicit_zero=False):
    basis=upstream(db,head,stage)
    if explicit_zero and not basis['ready']:raise ValueError('上游未就绪，不能确认整单为0')
    effective=rules.reconcile(raw_rows,basis['products'],pending=not basis['ready'])
    missing=[r['sku'] for r in effective if r['quantity_origin']=='missing'];extra=[r['sku'] for r in effective if r['outside_basis']]
    payload={'schema':SCHEMA,'rule':rules.RULE,'stage':stage,'head':{k:head[k] for k in ('oid','revision','warehouse','number','store','day','digest')},'basis':basis,'pending':not basis['ready'],'raw_rows':raw_rows,'rows':effective,'files':files,'missing':missing,'extra':extra,'explicit_zero':explicit_zero}
    digest=fingerprint(payload);old=current(db,head,stage);prior={r['sku']:r for r in old['payload']['rows']} if old else {}
    differences=[]
    for r in effective:
        a=prior.get(r['sku'],{})
        differences.append([head['number'],f"V{head['revision']:03d}",r['sku'],r['name'],r['upstream_quantity'],r['base_quantity'],r['difference'],r['processing_note'],a.get('base_quantity'),r['amount']])
    return {'head':head,'stage':stage,'expected':old['version'] if old else None,'digest':digest,'payload':payload,'changed':not old or old['digest']!=digest,'differences':differences,'duplicates':duplicates}

def match_payment_rows(path, head, rows):
    import re
    if not any(r.get('adapter')=='paid_order' for r in rows):return
    order=repository(path).get(head['day'],head['store'],head['revision'],view='export')
    if order['digest']!=head['digest']:raise wr.Conflict('签发快照已变化')
    barcodes={};other_codes=set()
    for d in order['payload']['result']['rows']:
        if ao.warehouse(d)!=head['warehouse']:other_codes.add(d['E']);continue
        for code in re.split(r'[,，;；\s]+',str(d.get('F') or '')):
            if code:barcodes.setdefault(code,set()).add(d['E'])
    for row in rows:
        if row.get('adapter')!='paid_order':continue
        original=row['sku'];aux=str(row['source_order'].get('辅助码') or '');hits=set()
        for code in re.split(r'[,，;；\s]+',aux):hits.update(barcodes.get(code,set()))
        if original in head['products']:
            if hits and hits!={original}:raise ValueError('商品编码与辅助码指向不同商品：'+original)
            row['matching_basis']='商品编码精确匹配'
        elif len(hits)==1:
            row['source_sku']=original;row['sku']=next(iter(hits));row['matching_basis']='辅助码唯一对应原签发条码'
        elif len(hits)>1:raise ValueError('辅助码对应多个商品，请核对：'+original)
        elif original in other_codes:raise ValueError('商品属于另一供货仓，请核对目标仓单：'+original)
        else:row['matching_basis']='清单外商品，需确认'

def prepare(path, scanned, bindings, progress=lambda _: None, cancel=None):
    """Bindings are exact whole-document heads, no per-line quantity allocations."""
    stage = scanned['stage']
    if stage not in STAGES: raise ValueError('未知生命周期环节')
    groups = {}; plans = []
    with closing(purchases.connect(path)) as db:
        for row in scanned['rows']:
            checkpoint(cancel); head = bindings.get(binding_key(row))
            if not head or not matching(row, [head]): raise ValueError('单据未明确匹配，或门店/仓/单号/修订不一致')
            if head['warehouse'] not in purchases.wo.WAREHOUSES: raise ValueError('旧单仓归属不明确，不能猜测关联')
            purchases.validate_head(db, head)
            groups.setdefault(key(head), {'head': head, 'rows': []})['rows'].append(copy.deepcopy(row))
        for i, group in enumerate(groups.values()):
            checkpoint(cancel); h = group['head']; unique = {}; duplicates = 0
            match_payment_rows(path,h,group['rows'])
            if len({(r['document_number'], r['document_date']) for r in group['rows']}) != 1: raise ValueError('一张仓单同一环节须提供一份完整单据；不同单号/日期不能自动拼成整单')
            for r in group['rows']:
                sku = r['sku']; r['sources'] = [r.pop('source')]
                if sku in unique:
                    before = unique[sku]
                    facts = ('quantity', 'unit', 'factor', 'conversion_basis', 'base_quantity', 'amount', 'currency', 'note', 'name')
                    if any(wr.dumps(before[k]) != wr.dumps(r[k]) for k in facts): raise ValueError('同仓单同商品内容冲突：' + sku + '；请整理为每商品一行')
                    before['sources'].extend(r['sources']); duplicates += 1
                else: unique[sku] = r
            rows = list(unique.values()); used = {s['sha'] for r in rows for s in r['sources']}
            plans.append(make_plan(db,h,stage,rows,[f for f in scanned['files'] if f['sha'] in used],duplicates))
            progress(RowProgress(f'核对整单 {i+1}/{len(groups)}：' + h['number'], i+1, len(groups)))
    if not plans: raise ValueError('没有可保存的整单')
    return {'plans': plans, 'files': scanned['files']}

def recheck(path, head, stage, progress=lambda _:None, cancel=None):
    checkpoint(cancel)
    with closing(purchases.connect(path)) as db:
        purchases.validate_head(db,head);doc=current(db,head,stage)
        if not doc:raise ValueError('尚无此环节单据')
        p=doc['payload'];raw=copy.deepcopy(p.get('raw_rows',p['rows']))
        plan=make_plan(db,head,stage,raw,p['files'],explicit_zero=p.get('explicit_zero',False))
    return {'plans':[plan],'files':p['files'],'archived_sources':True}

def zero_plan(path, head, stage, progress=lambda _:None, cancel=None):
    checkpoint(cancel)
    with closing(purchases.connect(path)) as db:
        purchases.validate_head(db,head);plan=make_plan(db,head,stage,[],[],explicit_zero=True)
    return {'plans':[plan],'files':[]}

def begin(db, cancel):
    start = time.monotonic()
    while True:
        checkpoint(cancel)
        try: db.execute('BEGIN IMMEDIATE'); checkpoint(cancel); return
        except sqlite3.OperationalError as exc:
            if 'locked' not in str(exc).lower() or time.monotonic()-start > 10: raise

def commit(path, plan, progress=lambda _: None, cancel=None):
    checkpoint(cancel); contents = []
    for i, f in enumerate(plan['files']):
        checkpoint(cancel)
        if plan.get('archived_sources'):
            with closing(purchases.connect(path)) as db:
                row=db.execute('SELECT content FROM lifecycle_files WHERE sha=?',(f['sha'],)).fetchone()
            if not row:raise ValueError('历史原件缺失')
            raw=row[0]
        else:raw = Path(f['path']).read_bytes()
        if hashlib.sha256(raw).hexdigest() != f['sha']: raise ValueError('原文件在预览后变化，请重新读取')
        contents.append((f, raw)); progress(RowProgress(f'留存原件 {i+1}/{len(plan["files"])}', i+1, len(plan['files'])))
    encoded = [wr.dumps(p['payload']) for p in plan['plans']]; checkpoint(cancel)
    import storage_chunks
    storage_chunks.backup_before_write(Path(path), progress); checkpoint(cancel)
    receipts = []
    with closing(sqlite3.connect(path, timeout=.15)) as db, db:
        begin(db, cancel); schema(db)
        for p in plan['plans']:
            checkpoint(cancel); purchases.validate_head(db, p['head']); old = current(db, p['head'], p['stage'])
            if (old['version'] if old else None) != p['expected']: raise wr.Conflict('单据在预览后被更新，请重新核对')
            if p['payload']['basis'] != upstream(db,p['head'],p['stage']):raise wr.Conflict('上游单据在预览后变化，请重新核对')
            if fingerprint(p['payload']) != p['digest']: raise ValueError('预览内容变化')
        # Last checkpoint. Never terminate the process between here and COMMIT.
        checkpoint(cancel); progress(COMMIT_BOUNDARY)
        for f, raw in contents: db.execute('INSERT OR IGNORE INTO lifecycle_files VALUES(?,?,?)', (f['sha'], f['name'], raw))
        for p, raw in zip(plan['plans'], encoded):
            v = p['expected'] or 0; h = p['head']
            if p['changed']:
                v += 1; db.execute('INSERT INTO lifecycle_versions VALUES(?,?,?,?,?,?,?,?)', (*key(h), p['stage'], v, wr.now(), p['digest'], raw))
                db.execute('INSERT INTO lifecycle_events(at,oid,revision,warehouse,stage,version,action,detail) VALUES(?,?,?,?,?,?,?,?)', (wr.now(), *key(h), p['stage'], v, '确认完整单据', wr.dumps({'number': h['number'], 'rows': len(p['payload']['rows']), 'missing': p['payload']['missing'], 'extra': p['payload']['extra']})))
            receipts.append({'number': h['number'], 'revision': h['revision'], 'stage': p['stage'], 'version': v, 'changed': p['changed']})
    return receipts

def state(present):
    for i, stage in enumerate(STAGES):
        if not present.get(stage): return STATUSES[i], stage
    return STATUSES[-1], None

def document_summary(doc):
    if not doc:return None
    p=doc['payload']
    return {'version':doc['version'],'digest':doc['digest'],'schema':p.get('schema',1),'pending':p.get('pending',False),'dependencies':p.get('basis',{}).get('dependencies',{}),'positive':len(rules.positive(p['rows']))}

def progress_state(summaries, legacy=None):
    summaries=dict(summaries)
    if not summaries['return'] and legacy and legacy['complete']:
        summaries['return']={'version':legacy['version'],'digest':legacy['digest'],'schema':1,'positive':sum(v>0 for v in legacy['values'].values())}
    health={};tokens={}
    for s in STAGES:
        d=summaries[s]
        health[s]=bool(d) and (d['schema']==1 or not d['pending'] and d['dependencies']==tokens and all(health.values()))
        tokens[s]=rules.basis_token(d)
    if not summaries['return'] and not (legacy and legacy['complete']):return '待回传','return','',health
    for i,s in enumerate(STAGES[1:],1):
        prev=summaries[STAGES[i-1]]
        if not summaries[s]:
            if prev and health[STAGES[i-1]] and prev['schema']==SCHEMA and prev['positive']==0 and not any(summaries[k] for k in STAGES[i:]):
                return STATUSES[-1],None,'无要货，无需付款/收货' if s=='payment' else '无付款数量，无需收货',health
            return STATUSES[i],s,'',health
        if not health[s]:return STATUSES[i],s,TITLES[s]+'对照依据已更新或未就绪，请重新核对',health
    return STATUSES[-1],None,'单据已收齐；不代表数量完全一致',health

def legacy_return(db, head, products):
    old = merchant_feedback.current(db, head['oid'], head['revision'])
    if not old: return None
    values = old['payload'].get('values', {}); required = [s for s, p in products.items() if p['quantity'] > 0]
    supplied = {s: q for s, q in values.items() if s in products}
    complete = bool(supplied) and all(s in supplied for s in required)
    return {'version': old['version'], 'saved': old['saved'], 'complete': complete, 'values': supplied, 'legacy': True,'digest':hashlib.sha256(wr.dumps(supplied).encode()).hexdigest()}

def snapshot(path, head, progress=lambda _: None, cancel=None, db=None):
    if db is None:
        with closing(purchases.connect(path)) as connection:
            connection.execute('BEGIN'); return snapshot(path, head, progress, cancel, connection)
    order = repository(path).get(head['day'], head['store'], head['revision'], view='export')
    if order['digest'] != head['digest']: raise wr.Conflict('签发快照不一致')
    eligible = {d['E']: {'sku': d['E'], 'name': d.get('D'), 'quantity': normal_exports.sem.order_quantity(d)} for rs in normal_exports.groups(order['payload']['result'], order['payload'].get('new_focus')).values() for d in rs if ao.warehouse(d) == head['warehouse']}
    docs = {s: current(db, head, s) for s in STAGES}; legacy = legacy_return(db, head, eligible)
    maps = {s: {r['sku']: r for r in doc['payload']['rows']} if doc else {} for s, doc in docs.items()}
    if not docs['return'] and legacy: maps['return'] = {s: {'base_quantity': str(q), 'name': eligible[s]['name']} for s, q in legacy['values'].items()}
    skus = list(eligible)
    for stage in STAGES:
        skus.extend(s for s in maps[stage] if s not in skus)
    rows = []
    for sku in skus:
        checkpoint(cancel); values = [maps[s].get(sku, {}).get('base_quantity') for s in STAGES]
        name = eligible.get(sku, {}).get('name') or next((maps[s][sku]['name'] for s in STAGES if sku in maps[s]), '')
        nums = [eligible.get(sku, {}).get('quantity'), *[float(Decimal(v)) if v is not None else None for v in values]]
        diffs = [nums[i+1]-nums[i] if nums[i] is not None and nums[i+1] is not None else None for i in range(3)]
        missing = '；'.join(TITLES[s] + ('未导入' if not docs[s] and not (s == 'return' and legacy) else '未列出此商品（旧口径）' if docs[s] and docs[s]['payload'].get('schema')==1 else '不在本环节对照范围') for s in STAGES if sku not in maps[s])
        notes=[TITLES[s]+'：'+maps[s][sku]['processing_note'] for s in STAGES if sku in maps[s] and maps[s][sku].get('processing_note')]
        missing='；'.join([missing,*notes]).strip('；')
        rows.append([sku, name, *nums, *diffs, maps['payment'].get(sku, {}).get('amount'), maps['payment'].get(sku, {}).get('currency'), missing])
    present = {s: bool(docs[s]) for s in STAGES}
    if not present['return'] and legacy: present['return'] = legacy['complete']
    status,next_stage,note,health=progress_state({s:document_summary(docs[s]) for s in STAGES},legacy)
    return {'head': head, 'rows': rows, 'documents': docs, 'legacy_return': legacy, 'present': present, 'status': status, 'next': next_stage,'note':note,'health':health}

HEADERS = ['商品编码', '商品名称', '门店推荐数（签发时）', '门店确认数', '付款单商品数量', '实际收货数', '确认−推荐', '付款−确认', '收货−付款', '付款实际金额（源单）', '币种', '资料说明']

def attachment(path, head, stage, version, sha, target, progress=lambda _: None, cancel=None):
    with closing(purchases.connect(path)) as db:
        doc = current(db, head, stage, version)
        if not doc or sha not in {f['sha'] for f in doc['payload']['files']}: raise ValueError('原件不属于此单据版本')
        row = db.execute('SELECT content FROM lifecycle_files WHERE sha=?', (sha,)).fetchone()
    if not row or hashlib.sha256(row[0]).hexdigest() != sha: raise ValueError('原文件缺失或校验失败')
    checkpoint(cancel); fd, tmp = tempfile.mkstemp(dir=Path(target).parent)
    try:
        with os.fdopen(fd, 'wb') as f: f.write(row[0])
        checkpoint(cancel); os.link(tmp, target)
    finally: Path(tmp).unlink(missing_ok=True)

def export(path, heads, folder, kind='comparison', mode='separate', progress=lambda _: None, cancel=None):
    """New exports only. Existing signed/calculation/master exporters untouched."""
    from xlsx_exports import stream_book, write_table
    if kind not in ('comparison', 'payment', 'receipt'): raise ValueError('未知导出类型')
    if mode not in ('separate', 'combined') or not heads: raise ValueError('请选择导出范围')
    completed = []; title = '生命周期对照' if kind == 'comparison' else TITLES[kind]
    try:
        views = []
        with closing(purchases.connect(path)) as db:
            db.execute('BEGIN')
            for h in heads:
                checkpoint(cancel); ao.require_active(db, h['oid'], h['warehouse']); view = snapshot(path, h, progress, cancel, db)
                if kind != 'comparison' and not view['documents'][kind]: raise ValueError(h['number'] + '尚未导入' + title)
                views.append(view)
        groups = [[v] for v in views] if mode == 'separate' else [[v for v in views if v['head']['warehouse'] == wh] for wh in dict.fromkeys(v['head']['warehouse'] for v in views)]
        for group in groups:
            checkpoint(cancel); rows = []
            for v in group:
                h = v['head']; prefix = [h['day'], h['store'], h['warehouse'], h['number'], h['revision']]
                if kind == 'comparison':
                    refs = [v['documents'][s]['version'] if v['documents'][s] else ('旧回传V' + str(v['legacy_return']['version']) if s == 'return' and v['legacy_return'] else None) for s in STAGES]
                    rows.extend([*prefix, *refs, *r] for r in v['rows'])
                else:
                    doc = v['documents'][kind]
                    for r in doc['payload']['rows']:
                        rows.append([*prefix, doc['version'], r['document_number'], r['document_date'], r['sku'], r['name'], float(Decimal(r['base_quantity'])), '库存单位', float(Decimal(r['amount'])) if r['amount'] is not None else None, r['currency'], r['quantity'], r['unit'], r['factor'], r['conversion_basis'], r['note']])
            headers = ['业务日期', '门店编码', '仓', '补货单号', '签发修订'] + (['回传版本', '付款版本', '收货版本'] + HEADERS if kind == 'comparison' else ['单据版本', DOCNUM[kind], DOCDATE[kind], '商品编码', '商品名称', QUANTITIES[kind], '数量单位', '实际金额', '币种', '源单数量', '源单单位', '换算系数', '换算依据', '备注'])
            target = Path(folder) / (title + '-' + (group[0]['head']['number'] if mode == 'separate' else '多订单') + '-' + uuid.uuid4().hex[:8] + '.xlsx')
            fd, tmp = tempfile.mkstemp(dir=target.parent, suffix='.xlsx'); os.close(fd)
            try:
                with stream_book() as book:
                    write_table(book, title, headers, rows, cancel=cancel, progress=progress, row_count=len(rows)); book.save(tmp)
                checkpoint(cancel); os.link(tmp, target); completed.append(str(target))
            finally: Path(tmp).unlink(missing_ok=True)
        return {'files': completed, 'cancelled': False, 'error': None}
    except Cancelled: return {'files': completed, 'cancelled': True, 'error': None}
    except Exception as exc: return {'files': completed, 'cancelled': False, 'error': str(exc)}
