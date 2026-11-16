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
import lifecycle_duplicates as duplicate_rows
from history_read import repository
from runtime_support import checkpoint, Cancelled
from export_progress import RowProgress
from lifecycle_io import STAGES, TITLES, QUANTITIES, DOCNUM, DOCDATE, fields
from lifecycle_io import ALL_STAGES
import operations_flow as flow

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
    # Only file paths are projected out. Nested business data is read-only here;
    # copying every raw cell doubled peak memory without changing the digest.
    value = dict(payload)
    value['files'] = [{k: f[k] for k in ('name', 'sha')} for f in value['files']]
    return hashlib.sha256(wr.dumps(value).encode()).hexdigest()

def business_fingerprint(payload):
    """Equality for a new import only; persisted snapshot hashes stay unchanged."""
    evidence={'source','sources','raw_headers','raw_values'}
    value={k:v for k,v in payload.items() if k not in ('files','raw_rows','rows')}
    for key in ('raw_rows','rows'):
        value[key]=[{k:v for k,v in row.items() if k not in evidence} for row in payload.get(key,[])]
    return hashlib.sha256(wr.dumps(value).encode()).hexdigest()

def decode(row):
    if not row: return None
    version, saved, digest, raw = row; payload = wr.loads(raw)
    if payload.get('schema') not in (1,SCHEMA) or fingerprint(payload) != digest: raise ValueError('生命周期单据快照校验失败')
    return {'version': version, 'saved': saved, 'digest': digest, 'payload': payload}

def current(db, head, stage, version=None,*,as_of=None,_proofs=None,_verified=None):
    if not exists(db): return None
    condition = ' AND version=?' if version is not None else ''
    args = (*key(head), stage, *((version,) if version is not None else ()))
    if as_of:condition+=' AND julianday(saved)<=julianday(?)';args=(*args,as_of)
    row=db.execute('SELECT version,saved,digest,payload FROM lifecycle_versions WHERE oid=? AND revision=? AND warehouse=? AND stage=?' + condition + ' ORDER BY version DESC LIMIT 1', args).fetchone()
    if type(_verified) is dict and row:
        from lifecycle_read_proofs import reuse
        reused=reuse(head,stage,row,_verified.get(stage))
        if reused is not None:
            if _proofs is not None:_proofs[stage]=_verified[stage]
            return reused
    document=decode(row)
    if _proofs is not None and document:
        from lifecycle_read_proofs import make
        proof=make(head,stage,row,document)
        if proof is not None:_proofs[stage]=proof
    return document

def history(path, head, stage, progress=lambda _: None, cancel=None):
    with closing(purchases.connect(path)) as db:
        if not exists(db): return []
        result = []
        for row in db.execute('SELECT version,saved,digest,payload FROM lifecycle_versions WHERE oid=? AND revision=? AND warehouse=? AND stage=? ORDER BY version DESC', (*key(head), stage)):
            checkpoint(cancel);doc=decode(row);files=attachment_files(db,head,stage,doc)
            if {f['sha'] for f in files}-{f['sha'] for f in doc['payload']['files']}:doc['attachments']=files
            result.append(doc)
        return result

def attachment_files(db,head,stage,doc):
    files={f['sha']:f for f in doc['payload']['files']}
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='lifecycle_events'").fetchone():return list(files.values())
    for raw, in db.execute('SELECT detail FROM lifecycle_events WHERE oid=? AND revision=? AND warehouse=? AND stage=? AND version=? AND action=? ORDER BY id',(*key(head),stage,doc['version'],'业务未变化，留存新原件')):
        for f in wr.loads(raw)['files']:files.setdefault(f['sha'],f)
    return list(files.values())

def mappings(path, stage, name=None, value=None, progress=lambda _: None, cancel=None):
    checkpoint(cancel)
    reserved=('payment_shop_names.','payment_match.','jst_order_reference.')
    if value is not None and stage=='payment' and name.startswith(reserved):raise ValueError('该名称由独立付款资料维护使用，请更换字段映射方案名称')
    if value is not None:
        import storage_chunks
        storage_chunks.backup_before_write(Path(path), progress)
    with closing(purchases.connect(path)) as db, db:
        if value is not None:
            checkpoint(cancel); schema(db)
            db.execute('INSERT INTO lifecycle_mappings VALUES(?,?,?) ON CONFLICT(stage,name) DO UPDATE SET payload=excluded.payload', (stage, name, wr.dumps(value)))
        if not exists(db): return {}
        return {n: wr.loads(p) for n, p in db.execute('SELECT name,payload FROM lifecycle_mappings WHERE stage=? ORDER BY name', (stage,)) if stage!='payment' or not n.startswith(reserved)}

def binding_key(row):
    return wr.dumps([row.get(k) for k in ('signed_number', 'revision', 'store', 'warehouse', 'document_number')])

def matching(row, heads):
    return [h for h in heads if all(not row.get(f) or row[f] == h[k] for f, k in (('signed_number', 'number'), ('revision', 'revision'), ('store', 'store'), ('warehouse', 'warehouse')))]

def available(path, scanned, progress=lambda _: None, cancel=None):
    scopes = set(); unknown = False
    with closing(purchases.connect(path)) as db:
        resolved={};data_version=None
        for row in scanned['rows']:
            checkpoint(cancel)
            if row['signed_number']:
                version=db.execute('PRAGMA data_version').fetchone()[0]
                if version!=data_version:resolved.clear();data_version=version
                number=row['signed_number']
                try:hit=resolved.get(number)
                except TypeError:hit=None
                if hit is None:
                    hit=purchases.wo.resolve(db,number)
                    after=db.execute('PRAGMA data_version').fetchone()[0]
                    if after==version:
                        try:resolved[number]=hit
                        except TypeError:pass
                    else:resolved.clear();data_version=after
                identity, wh = hit
                scopes.update((identity[1], w) for w in ([wh] if wh else purchases.wo.WAREHOUSES))
            elif row['store']:
                scopes.update((row['store'], w) for w in ([row['warehouse']] if row['warehouse'] else purchases.wo.WAREHOUSES))
            else: unknown = True
    return purchases.targets(path, None if unknown else scopes, progress, cancel)

def current_targets(path, progress=lambda _:None, cancel=None):
    return [h for h in purchases.targets(path,progress=progress,cancel=cancel) if h['revision']==h['current_revision']]

def prepare_numbered(path, scanned, scope=None, progress=lambda _:None, cancel=None, *, staged=None,_owned=False):
    """Public import: one order number, current signed facts; versions stay internal.

    Existing prepare/recheck remain exact-snapshot compatibility APIs. A current
    head is frozen here; validate_head still rejects re-signing after preview.
    """
    # The batch entry already owns an isolated copy. Public callers still get
    # one private snapshot; neither UI scans nor cached inputs are mutated.
    if not _owned:scanned=copy.deepcopy(scanned)
    for row in scanned['rows']:
        checkpoint(cancel)
        if not row.get('signed_number'):
            if scope:row['signed_number']=scope['number']
            else:raise ValueError('缺少补货单号：批量导入须填写；单张订单可从该订单详情进入，由系统带入')
        if scope and row['signed_number']!=scope['number']:raise ValueError('补货单号与所选订单不一致')
        if not row.get('identity_reference'):row['revision']=None
    if scope:
        with closing(purchases.connect(path)) as db:purchases.validate_head(db,scope)
        h=full_head(path,scope)
        if not h['products']:raise ValueError('所选签发中不存在该仓的业务明细')
        heads=[h] if h['revision']==h['current_revision'] else []
    else:heads=[h for h in available(path,scanned,progress,cancel) if h['revision']==h['current_revision']]
    bindings={}
    for row in scanned['rows']:
        checkpoint(cancel)
        choices=[h for h in heads if h['number']==row['signed_number']]
        if len(choices)!=1:raise ValueError('补货单号未对应唯一有效仓单：'+row['signed_number']+'；请使用分仓补货单号')
        h=choices[0]
        reference=row.get('identity_reference')
        candidates=row.get('identity_candidates')
        if candidates:
            refs=[r for r in candidates if r['number']==h['number']]
            if len(refs)!=1:raise ValueError('所选订单不在隐藏页的候选范围内')
            reference=refs[0];row['identity_reference']=reference
        if reference and any(reference[k]!=h[k] for k in ('number','store','day','revision','digest')):raise wr.Conflict('原签发已变化，不能把旧文件静默关联到新签发：'+h['number'])
        if reference and reference['warehouse'] and reference['warehouse']!=h['warehouse']:raise ValueError('隐藏页供货仓与目标订单不一致')
        if not matching(row,[h]):raise ValueError('源表门店或仓库与补货单号不一致：'+row['signed_number'])
        row['revision']=h['revision']
        bindings[binding_key(row)]=h
    return prepare(path,scanned,bindings,progress,cancel,staged=staged,_owned=True)

def upstream(db, head, stage, staged=None,*,_proofs=None,_verified=None):
    """Freeze every dependency; absence is also a token checked at commit."""
    route=flow.stages(db,head,staged,including=stage)
    index=route.index(stage); tokens={}; previous=None; valid=True
    for s in route[:index]:
        doc=(staged or {}).get((*key(head),s)) or current(db,head,s,_proofs=_proofs,_verified=_verified)
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
    return {'stage':'signed' if stage=='return' else route[index-1], 'dependencies':tokens, 'signed_digest':head['digest'], 'ready':stage=='return' or valid, 'products':products}

def make_plan(db, head, stage, raw_rows, files, duplicates=0, explicit_zero=False, *, staged=None,_verified=None):
    proofs={}
    basis=upstream(db,head,stage,staged,_proofs=proofs,_verified=_verified)
    if explicit_zero and not basis['ready']:raise ValueError('上游未就绪，不能确认整单为0')
    effective=rules.reconcile(raw_rows,basis['products'],pending=not basis['ready'])
    missing=[r['sku'] for r in effective if r['quantity_origin']=='missing'];extra=[r['sku'] for r in effective if r['outside_basis']]
    payload={'schema':SCHEMA,'rule':rules.RULE,'stage':stage,'head':{k:head[k] for k in ('oid','revision','warehouse','number','store','day','digest')},'basis':basis,'pending':not basis['ready'],'raw_rows':raw_rows,'rows':effective,'files':files,'missing':missing,'extra':extra,'explicit_zero':explicit_zero}
    if stage=='payment':
        kinds=list(dict.fromkeys(r.get('adapter','payment_file') for r in raw_rows))
        if 'jst_import_return' in kinds:
            payload['payment_source']={'type':'jst_import_return','external_order_number':None,'amount_basis':'数量×单价'}
    digest=fingerprint(payload);old=current(db,head,stage,_proofs=proofs);prior={r['sku']:r for r in old['payload']['rows']} if old else {}
    differences=[]
    for r in effective:
        a=prior.get(r['sku'],{})
        differences.append([head['number'],f"V{head['revision']:03d}",r['sku'],r['name'],r['upstream_quantity'],r['base_quantity'],r['difference'],r['processing_note'],a.get('base_quantity'),r['amount']])
    calibration=[]
    if old:
        after={r['sku']:r for r in effective}
        for sku in dict.fromkeys([*prior,*after]):
            a=prior.get(sku,{});b=after.get(sku,{})
            if any(a.get(k)!=b.get(k) for k in ('base_quantity','amount','document_number')):
                calibration.append([sku,b.get('name') or a.get('name'),a.get('base_quantity'),b.get('base_quantity'),a.get('amount'),b.get('amount'),a.get('document_number'),b.get('document_number')])
    evidence=None
    if old and old['digest']!=digest and business_fingerprint(old['payload'])==business_fingerprint(payload):
        evidence={'files':[{k:f[k] for k in ('name','sha')} for f in files],'source_digest':digest}
        # Downstream dependencies must refer to the exact retained version, not
        # a hypothetical digest containing different source cells or filenames.
        payload=old['payload'];digest=old['digest']
    plan={'head':head,'stage':stage,'expected':old['version'] if old else None,'digest':digest,'payload':payload,'changed':not old or old['digest']!=digest,'differences':differences,'duplicates':duplicates,'calibration':calibration}
    if evidence:plan['source_evidence']=evidence
    if proofs:plan['_read_proofs']=proofs
    return plan

def match_payment_rows(path, head, rows):
    if not any(r.get('adapter')=='paid_order' for r in rows):return
    identities={tuple(str(r['source_order'].get(k) or '') for k in ('内部订单号','线上订单号','店铺名称','买家账号','平台站点')) for r in rows if r.get('adapter')=='paid_order'}
    if len(identities)!=1:raise ValueError('付款文件含多个来源订单/店铺，请分别导入，不自动合并')
    from warehouse_execution import get_order
    order=get_order(repository(path),head,view='export')
    if order['digest']!=head['digest']:raise wr.Conflict('签发快照已变化')
    for row in rows:
        if row.get('adapter')!='paid_order':continue
        original=row['sku']
        if original in head['products']:
            row['matching_basis']='商品编码精确匹配'
        else:row['matching_basis']='清单外商品，需确认'

def prepare(path, scanned, bindings, progress=lambda _: None, cancel=None, *, staged=None,_owned=False):
    """Bindings are exact whole-document heads, no per-line quantity allocations."""
    stage = scanned['stage']
    if stage not in ALL_STAGES: raise ValueError('未知生命周期环节')
    groups = {}; plans = []
    with closing(purchases.connect(path)) as db:
        checked=set();data_version=None
        for row in scanned['rows']:
            checkpoint(cancel); head = bindings.get(binding_key(row))
            if not head or not matching(row, [head]): raise ValueError('单据未明确匹配，或门店/仓/单号/修订不一致')
            if head['warehouse'] not in purchases.wo.WAREHOUSES: raise ValueError('旧单仓归属不明确，不能猜测关联')
            version=db.execute('PRAGMA data_version').fetchone()[0]
            if version!=data_version:checked.clear();data_version=version
            token=tuple((k in head,head.get(k)) for k in ('oid','revision','warehouse','digest','current_revision','execution_epoch','visibility_token'))
            try:known=token in checked
            except TypeError:known=False;token=None
            if not known:
                purchases.validate_head(db, head)
                after=db.execute('PRAGMA data_version').fetchone()[0]
                if after==version and token is not None:checked.add(token)
                else:checked.clear();data_version=after
            groups.setdefault(key(head), {'head': head, 'rows': []})['rows'].append(row if _owned else copy.deepcopy(row))
        for i, group in enumerate(groups.values()):
            checkpoint(cancel); h = group['head']; unique = {}; duplicates = 0
            purchases.validate_head(db,h)  # Latest state when freezing each plan.
            match_payment_rows(path,h,group['rows'])
            if len({(r['document_number'], r['document_date']) for r in group['rows']}) != 1: raise ValueError('一张仓单同一环节须提供一份完整单据；不同单号/日期不能自动拼成整单')
            conflicts = duplicate_rows.find(group['rows'])
            if conflicts: raise ValueError(duplicate_rows.message(conflicts))
            for r in group['rows']:
                sku = r['sku']; r['sources'] = [r.pop('source')]
                if sku in unique:
                    before = unique[sku]
                    facts = ('quantity', 'unit', 'factor', 'conversion_basis', 'base_quantity', 'amount', 'currency', 'note', 'name')
                    if any(wr.dumps(before[k]) != wr.dumps(r[k]) for k in facts): raise ValueError('同仓单同商品内容冲突：' + sku + '；请重新核对原始明细')
                    before['sources'].extend(r['sources']); duplicates += 1
                else: unique[sku] = r
            rows = list(unique.values()); used = {s['sha'] for r in rows for s in r['sources']}
            plans.append(make_plan(db,h,stage,rows,[f for f in scanned['files'] if f['sha'] in used],duplicates,staged=staged))
            progress(RowProgress(f'核对整单 {i+1}/{len(groups)}：' + h['number'], i+1, len(groups)))
    if not plans: raise ValueError('没有可保存的整单')
    return {'plans': plans, 'files': scanned['files']}

def recheck(path, head, stage, progress=lambda _:None, cancel=None):
    checkpoint(cancel)
    head=full_head(path,head)
    with closing(purchases.connect(path)) as db:
        purchases.validate_head(db,head);doc=current(db,head,stage)
        if not doc:raise ValueError('尚无此环节单据')
        p=doc['payload'];raw=copy.deepcopy(p.get('raw_rows',p['rows']))
        plan=make_plan(db,head,stage,raw,p['files'],explicit_zero=p.get('explicit_zero',False))
    return {'plans':[plan],'files':p['files'],'archived_sources':True}

def zero_plan(path, head, stage, progress=lambda _:None, cancel=None):
    checkpoint(cancel)
    head=full_head(path,head)
    with closing(purchases.connect(path)) as db:
        purchases.validate_head(db,head);plan=make_plan(db,head,stage,[],[],explicit_zero=True)
    return {'plans':[plan],'files':[]}

def full_head(path,head):
    if 'products' in head:return head
    from warehouse_execution import get_order
    order=get_order(repository(path),head,view='export')
    if order['digest']!=head['digest']:raise wr.Conflict('签发快照已变化')
    result=order['payload']['result']
    products={d['E']:{'sku':d['E'],'name':d.get('D'),'quantity':normal_exports.sem.order_quantity(d),'unit':d.get('G'),'price':d.get('V')} for rs in normal_exports.groups(result,order['payload'].get('new_focus')).values() for d in rs if ao.warehouse(d)==head['warehouse']}
    return {**head,'products':products}

def begin(db, cancel,*,deadline=None):
    start = time.monotonic()
    end=min(start+10,deadline) if deadline is not None else start+10
    while True:
        checkpoint(cancel)
        try: db.execute('BEGIN IMMEDIATE'); checkpoint(cancel); return
        except sqlite3.OperationalError as exc:
            if 'locked' not in str(exc).lower() or time.monotonic() > end: raise

def staged_document(plan):
    """Exact document that this plan will commit, including unchanged versions."""
    return {'version':(plan['expected'] or 0)+int(plan['changed']),
            'digest':plan['digest'],'payload':plan['payload']}


class _BusyCommit(Exception):
    def __init__(self,error):self.error=error


class _RollbackWait(str):
    active=False  # Safe rollback completed: cancellation is available again.


_SAVE_BUSY_SECONDS=10

def commit(path, plan, progress=lambda _: None, cancel=None):
    import lifecycle_recovery
    return lifecycle_recovery.execute(path,plan,progress,cancel,_commit)


def _commit(path, plan, progress=lambda _: None, cancel=None):
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
    checkpoint(cancel)
    import storage_chunks
    storage_chunks.backup_before_write(Path(path), progress); checkpoint(cancel)
    deadline=time.monotonic()+_SAVE_BUSY_SECONDS
    while True:
        try:return _commit_once(path,plan,contents,progress,cancel,deadline)
        except _BusyCommit as blocked:
            # COMMIT returned SQLITE_BUSY while the transaction was still live,
            # and rollback succeeded. Only this proven-uncommitted attempt can
            # be replayed, with all source/version/dependency checks repeated.
            if time.monotonic()>=deadline:raise blocked.error
            checkpoint(cancel)
            progress(_RollbackWait('本地读取短暂占用，已安全回滚本次尝试；正在重新核验后保存，不会重复新增'))
            time.sleep(.025)


def _commit_once(path,plan,contents,progress,cancel,deadline):
    receipts = []
    with closing(sqlite3.connect(path, timeout=.15)) as db, db:
        begin(db, cancel,deadline=deadline); schema(db)
        if plan.get('payment_matching'):
            from payment_matching import validate
            validate(path,db,plan)
        staged={};encoded=[]
        for p in plan['plans']:
            verified=p.get('_read_proofs')
            checkpoint(cancel); purchases.validate_head(db, p['head']); old = current(db, p['head'], p['stage'],_verified=verified)
            from jst_orders import guard_return,guard_payment
            if p['stage'] in ('return','operations') and p['changed']:guard_return(db,p['head'])
            if p['stage']=='payment':guard_payment(db,p)
            identity=(*key(p['head']),p['stage'])
            if identity in staged:raise ValueError('同一订单同一环节只能保存一份完整单据')
            if (old['version'] if old else None) != p['expected']: raise wr.Conflict('单据在预览后被更新，请重新核对')
            if p['payload']['basis'] != upstream(db,p['head'],p['stage'],staged,_verified=verified):raise wr.Conflict('上游单据在预览后变化，请重新核对')
            from lifecycle_save_encoding import encode
            raw,checked_digest=encode(p['payload'],retain=p['changed'])
            if checked_digest != p['digest']: raise ValueError('预览内容变化')
            if p['changed'] != (not old or old['digest']!=p['digest']):raise ValueError('预览更新状态变化')
            staged[identity]=staged_document(p)
            encoded.append(raw)
        # Last checkpoint. Never terminate the process between here and COMMIT.
        checkpoint(cancel); progress(COMMIT_BOUNDARY)
        for f, raw in contents: db.execute('INSERT OR IGNORE INTO lifecycle_files VALUES(?,?,?)', (f['sha'], f['name'], raw))
        for p, raw in zip(plan['plans'], encoded):
            v = p['expected'] or 0; h = p['head']
            if p['changed']:
                v += 1; db.execute('INSERT INTO lifecycle_versions VALUES(?,?,?,?,?,?,?,?)', (*key(h), p['stage'], v, wr.now(), p['digest'], raw))
                if p['stage']==flow.demand_stage(db,h):
                    from jst_tasks import enqueue
                    enqueue(db,h,{'version':v,'digest':p['digest']})
                db.execute('INSERT INTO lifecycle_events(at,oid,revision,warehouse,stage,version,action,detail) VALUES(?,?,?,?,?,?,?,?)', (wr.now(), *key(h), p['stage'], v, '确认完整单据', wr.dumps({'number': h['number'], 'rows': len(p['payload']['rows']), 'missing': p['payload']['missing'], 'extra': p['payload']['extra']})))
            evidence=p.get('source_evidence')
            if evidence:
                if p['changed'] or any(f not in [{k:f[k] for k in ('name','sha')} for f,_ in contents] for f in evidence['files']):raise ValueError('原件留存范围与核对文件不一致')
                detail=wr.dumps(evidence);action='业务未变化，留存新原件'
                if not db.execute('SELECT 1 FROM lifecycle_events WHERE oid=? AND revision=? AND warehouse=? AND stage=? AND version=? AND action=? AND detail=?',(*key(h),p['stage'],v,action,detail)).fetchone():
                    db.execute('INSERT INTO lifecycle_events(at,oid,revision,warehouse,stage,version,action,detail) VALUES(?,?,?,?,?,?,?,?)',(wr.now(),*key(h),p['stage'],v,action,detail))
            receipts.append({'number': h['number'], 'revision': h['revision'], 'stage': p['stage'], 'version': v, 'changed': p['changed']})
        if plan.get('payment_matching'):
            from payment_matching import apply
            apply(db,plan)
        try:db.commit()
        except sqlite3.OperationalError as exc:
            code=getattr(exc,'sqlite_errorcode',None)
            busy=(code & 255)==sqlite3.SQLITE_BUSY if isinstance(code,int) else str(exc).lower()=='database is locked'
            if not busy or not db.in_transaction:raise
            db.rollback()  # Failure here propagates: unknown outcomes never replay.
            raise _BusyCommit(exc) from exc
    return receipts

def state(present):
    for i, stage in enumerate(STAGES):
        if not present.get(stage): return STATUSES[i], stage
    return STATUSES[-1], None

def document_summary(doc):
    if not doc:return None
    p=doc['payload']
    return {'version':doc['version'],'digest':doc['digest'],'schema':p.get('schema',1),'pending':p.get('pending',False),'dependencies':p.get('basis',{}).get('dependencies',{}),'positive':len(rules.positive(p['rows']))}

def progress_state(summaries, legacy=None, operations_required=False):
    summaries=dict(summaries)
    route=flow.CURRENT if operations_required or summaries.get('operations') else STAGES
    labels={'return':'待回传','operations':'待运营签发','payment':'待付款','receipt':'待收货'}
    if not summaries['return'] and legacy and legacy['complete']:
        summaries['return']={'version':legacy['version'],'digest':legacy['digest'],'schema':1,'positive':sum(v>0 for v in legacy['values'].values())}
    health={};tokens={}
    for s in route:
        d=summaries.get(s)
        health[s]=bool(d) and (d['schema']==1 or not d['pending'] and d['dependencies']==tokens and all(health.values()))
        tokens[s]=rules.basis_token(d)
    if not summaries['return'] and not (legacy and legacy['complete']):return '待回传','return','',health
    for i,s in enumerate(route[1:],1):
        prev=summaries.get(route[i-1])
        if not summaries.get(s):
            if prev and health[route[i-1]] and prev['schema']==SCHEMA and prev['positive']==0 and not any(summaries.get(k) for k in route[i:]):
                return STATUSES[-1],None,('无要货，无需后续运营、分配、付款或收货' if route[i-1] in ('return','operations') else '无付款数量，无需收货'),health
            return labels[s],s,'',health
        if not health[s]:return labels[s],s,TITLES[s]+'对照依据已更新或未就绪，请重新核对',health
    return STATUSES[-1],None,'单据已收齐；不代表数量完全一致',health

def legacy_return(db, head, products,*,as_of=None):
    old = merchant_feedback.current(db, head['oid'], head['revision'],as_of=as_of)
    if not old: return None
    values = old['payload'].get('values', {}); required = [s for s, p in products.items() if p['quantity'] > 0]
    supplied = {s: q for s, q in values.items() if s in products}
    complete = bool(supplied) and all(s in supplied for s in required)
    return {'version': old['version'], 'saved': old['saved'], 'complete': complete, 'values': supplied, 'legacy': True,'digest':hashlib.sha256(wr.dumps(supplied).encode()).hexdigest()}

def _comparison_document(document):
    """Compact only after full decode/fingerprint verification; never persisted."""
    if document is None:return None
    payload=document['payload']
    rows=[{k:r[k] for k in ('sku','name','base_quantity','amount','currency','processing_note') if k in r} for r in payload['rows']]
    value={k:payload[k] for k in ('schema','pending') if k in payload}
    value['rows']=rows
    if 'basis' in payload:value['basis']={'dependencies':payload['basis'].get('dependencies',{})}
    return {**document,'payload':value}


def snapshot(path, head, progress=lambda _: None, cancel=None, db=None, *, order=None,as_of=None,_comparison=True):
    if db is None:
        with closing(purchases.connect(path)) as connection:
            connection.execute('BEGIN'); return snapshot(path, head, progress, cancel, connection, order=order,as_of=as_of,_comparison=_comparison)
    if order is None:
        from warehouse_execution import get_order
        order=get_order(repository(path),head,view='export')
    if order['digest'] != head['digest']: raise wr.Conflict('签发快照不一致')
    eligible = {d['E']: {'sku': d['E'], 'name': d.get('D'), 'quantity': normal_exports.sem.order_quantity(d)} for rs in normal_exports.groups(order['payload']['result'], order['payload'].get('new_focus')).values() for d in rs if ao.warehouse(d) == head['warehouse']}
    if not _comparison:del order  # The verified large signed tree is no longer needed.
    route=flow.stages(db,head,as_of=as_of)
    if _comparison:docs = {s: current(db, head, s,as_of=as_of) for s in route}
    else:
        docs={}
        for s in route:
            checkpoint(cancel);document=current(db,head,s,as_of=as_of)
            docs[s]=document if s=='payment' else _comparison_document(document)
            del document
    legacy = legacy_return(db, head, eligible,as_of=as_of)
    rows = []
    if _comparison:
        maps = {s: {r['sku']: r for r in doc['payload']['rows']} if doc else {} for s, doc in docs.items()}
        if not docs['return'] and legacy: maps['return'] = {s: {'base_quantity': str(q), 'name': eligible[s]['name']} for s, q in legacy['values'].items()}
        skus = list(eligible); seen = set(skus)
        for stage in STAGES:
            for sku in maps[stage]:
                if sku not in seen:
                    skus.append(sku); seen.add(sku)
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
    if 'operations' in docs:present['operations']=bool(docs['operations'])
    status,next_stage,note,health=progress_state({s:document_summary(docs[s]) for s in route},legacy,route==flow.CURRENT)
    return {'head': head, 'rows': rows, 'documents': docs, 'legacy_return': legacy, 'present': present, 'status': status, 'next': next_stage,'note':note,'health':health,'flow_version':2 if route==flow.CURRENT else 1}

HEADERS = ['商品编码', '商品名称', '门店推荐数（签发时）', '门店确认数', '付款单商品数量', '实际收货数', '确认−推荐', '付款−确认', '收货−付款', '付款实际金额（源单）', '币种', '资料说明']
FLOW_HEADERS=['商品编码','商品名称','补货签发数','门店回传数','运营签发数','付款单商品数量','实际收货数','回传−签发','运营−回传','付款−运营','收货−付款','付款实际金额（源单）','币种','资料说明']

def flow_rows(view):
    doc=view['documents'].get('operations');ops={r['sku']:r for r in doc['payload']['rows']} if doc else {}
    result=[]
    for r in view['rows']:
        q=ops.get(r[0],{}).get('base_quantity');q=float(Decimal(q)) if q is not None else 0 if doc and doc['payload'].get('schema')==SCHEMA else None
        nums=[r[2],r[3],q,r[4],r[5]]
        delta=[b-a if a is not None and b is not None else None for a,b in zip(nums,nums[1:])]
        result.append([*r[:2],*nums,*delta,*r[9:]])
    # Operations may explicitly add a SKU which was absent from the original
    # signature/return. Preserve it in comparison, rather than hiding the row.
    seen={r[0] for r in result}
    for sku,r in ops.items():
        if sku not in seen:result.append([sku,r.get('name',''),0,0,float(Decimal(r['base_quantity'])),None,None,0,float(Decimal(r['base_quantity'])),None,None,None,None,'运营追加商品'])
    return result

def attachment(path, head, stage, version, sha, target, progress=lambda _: None, cancel=None):
    with closing(purchases.connect(path)) as db:
        doc = current(db, head, stage, version)
        if not doc or sha not in {f['sha'] for f in attachment_files(db,head,stage,doc)}: raise ValueError('原件不属于此单据版本')
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
    if kind not in ('comparison', *ALL_STAGES): raise ValueError('未知导出类型')
    if mode not in ('separate', 'combined') or not heads: raise ValueError('请选择导出范围')
    completed = []; title = '生命周期对照' if kind == 'comparison' else TITLES[kind]
    try:
        views = []
        with closing(purchases.connect(path)) as db:
            db.execute('BEGIN')
            for h in heads:
                checkpoint(cancel); ao.require_active(db, h['oid'], h['warehouse']); view = snapshot(path, h, progress, cancel, db)
                if kind != 'comparison' and not view['documents'].get(kind): raise ValueError(h['number'] + '尚未导入' + title)
                views.append(view)
        if kind in ('return','operations'):
            from lifecycle_exports import export_returns
            return export_returns(path,views,folder,mode,progress,cancel,stage=kind)
        groups = [[v] for v in views] if mode == 'separate' else [[v for v in views if v['head']['warehouse'] == wh] for wh in dict.fromkeys(v['head']['warehouse'] for v in views)]
        for group in groups:
            checkpoint(cancel); rows = []
            full_flow=kind=='comparison' and any(v.get('flow_version')==2 for v in group)
            compare_stages=ALL_STAGES if full_flow else STAGES
            modern=kind!='comparison' and any(v['documents'][kind]['payload'].get('schema')==SCHEMA for v in group)
            for v in group:
                h = v['head']; prefix = [h['day'], h['store'], h['warehouse'], h['number'], h['revision']]
                if kind == 'comparison':
                    refs = [v['documents'][s]['version'] if v['documents'].get(s) else ('旧回传V' + str(v['legacy_return']['version']) if s == 'return' and v['legacy_return'] else None) for s in compare_stages]
                    rows.extend([*prefix, *refs, *r] for r in (flow_rows(v) if full_flow else v['rows']))
                else:
                    doc = v['documents'][kind]
                    for r in doc['payload']['rows']:
                        values=[*prefix, doc['version'], r['document_number']]+([] if modern and kind=='payment' else [r['document_date']])+[r['sku'], r['name'], float(Decimal(r['base_quantity'])), '库存单位', float(Decimal(r['amount'])) if r['amount'] is not None else None, r['currency'], r['quantity'], r['unit'], r['factor'], r['conversion_basis'], r['note']]
                        if modern:values.extend([r.get('upstream_quantity'),r.get('difference'),r.get('processing_note','历史旧口径'),v.get('note','')])
                        rows.append(values)
            body=['单据版本',DOCNUM.get(kind)]+([] if modern and kind=='payment' else [DOCDATE.get(kind)])+['商品编码','商品名称',QUANTITIES.get(kind),'数量单位','实际金额','币种','源单数量','源单单位','换算系数','换算依据','备注']
            if modern:body+=['上环节数量','与上环节差额','数量处理说明','对照依据提示']
            headers = ['业务日期', '门店编码', '仓', '补货单号', '签发修订'] + (([TITLES[s]+'版本' for s in compare_stages] if full_flow else ['回传版本','付款版本','收货版本'])+(FLOW_HEADERS if full_flow else HEADERS) if kind == 'comparison' else body)
            target = Path(folder) / (title + '-' + (group[0]['head']['number'] if mode == 'separate' else group[0]['head']['warehouse']+'-多订单') + '-' + uuid.uuid4().hex[:8] + '.xlsx')
            fd, tmp = tempfile.mkstemp(dir=target.parent, suffix='.xlsx'); os.close(fd)
            try:
                with stream_book() as book:
                    write_table(book, title, headers, rows, cancel=cancel, progress=progress, row_count=len(rows)); book.save(tmp)
                checkpoint(cancel); os.link(tmp, target); completed.append(str(target))
            finally: Path(tmp).unlink(missing_ok=True)
        return {'files': completed, 'cancelled': False, 'error': None}
    except Cancelled: return {'files': completed, 'cancelled': True, 'error': None}
    except Exception as exc: return {'files': completed, 'cancelled': False, 'error': str(exc)}
