"""Whole-order downstream facts, independently versioned from immutable signing.

No computation or implicit payment conversion. Old ledgers are read-only inputs.
"""
import copy, hashlib, os, sqlite3, tempfile, time, uuid
from contextlib import closing
from decimal import Decimal
from pathlib import Path
import work_repository as wr
import purchases, merchant_feedback, result_semantics as sem, archive_orders as ao
import payment_match_commit
import lifecycle_rules as rules
import lifecycle_duplicates as duplicate_rows
from normal_orders import read_repository as repository
from lifecycle_facts import current, upstream, legacy_return, legacy_return_record, full_head, match_payment_rows
from runtime_support import checkpoint, Cancelled
from export_progress import RowProgress
from lifecycle_io import STAGES, TITLES, QUANTITIES, DOCNUM, DOCDATE, fields
from lifecycle_io import ALL_STAGES
import lifecycle_routes as flow
from jst_order_guards import guard_operations, guard_payment
from jst_shipping import read as read_shipments
from jst_task_store import enqueue
from lifecycle_stages import FINISHED, PENDING, STATUSES, route as document_route
from lifecycle_stages import presence_state as state, progress_state
from lifecycle_rules import DOCUMENT_SCHEMA as SCHEMA, JST_ORDER, document_summary
from order_keys import warehouse_revision as key
import lifecycle_store as store
from lifecycle_store import attachment_files, decode, exists, fingerprint, schema
from sqlite_write import begin
COMMIT_BOUNDARY = '正在原子提交单据；提交完成前不可取消'

def business_fingerprint(payload):
    """Equality for a new import only; persisted snapshot hashes stay unchanged."""
    evidence={'source','sources','raw_headers','raw_values'}
    value={k:v for k,v in payload.items() if k not in ('files','raw_rows','rows')}
    for key in ('raw_rows','rows'):
        value[key]=[{k:v for k,v in row.items() if k not in evidence} for row in payload.get(key,[])]
    return hashlib.sha256(wr.dumps(value).encode()).hexdigest()

def history(path, head, stage, progress=lambda _: None, cancel=None):
    with closing(purchases.connect(path)) as db:
        if not exists(db): return []
        result = []
        for row in store.version_rows(db, head, stage):
            checkpoint(cancel);doc=decode(row);files=attachment_files(db,head,stage,doc)
            if {f['sha'] for f in files}-{f['sha'] for f in doc['payload']['files']}:doc['attachments']=files
            result.append(doc)
        return result

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
            store.save_mapping(db, stage, name, value)
        if not exists(db): return {}
        return store.mappings(db, stage, reserved)

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
                if data_version is None:data_version=db.execute('PRAGMA data_version').fetchone()[0]
                number=row['signed_number']
                try:hit=resolved.get(number)
                except TypeError:hit=None
                if hit is None:
                    hit=purchases.wo.resolve(db,number)
                    try:resolved[number]=hit
                    except TypeError:pass
                identity, wh = hit
                scopes.update((identity[1], w) for w in ([wh] if wh else purchases.wo.WAREHOUSES))
            elif row['store']:
                scopes.update((row['store'], w) for w in ([row['warehouse']] if row['warehouse'] else purchases.wo.WAREHOUSES))
            else: unknown = True
        # Observe changes at this read boundary, not once per SKU. No read
        # transaction is held while grouping rows. A concurrent rename/delete
        # still goes through the real resolver; unrelated writes can pass.
        if resolved and db.execute('PRAGMA data_version').fetchone()[0]!=data_version:
            for number in resolved:
                checkpoint(cancel);identity,wh=purchases.wo.resolve(db,number)
                scopes.update((identity[1],w) for w in ([wh] if wh else purchases.wo.WAREHOUSES))
    return purchases.targets(path, None if unknown else scopes, progress, cancel)

def current_targets(path, progress=lambda _:None, cancel=None, *, numbers=None):
    return purchases.targets(path,progress=progress,cancel=cancel,current_only=True,numbers=numbers)

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

def make_plan(db, head, stage, raw_rows, files, duplicates=0, explicit_zero=False, *, staged=None,_verified=None,
              aoxiang_payment_review=None):
    if stage in ('return','operations'):
        from material_stock import has_material, is_material
        if has_material(db) and is_material(db, head['oid']):
            raise ValueError('物料单按已确认的物料需求和分配留存。请从「物料单」处理原申请')
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
    if stage=='receipt' and aoxiang_payment_review:
        payload['aoxiang_payment_review']=aoxiang_payment_review
    if round_:=store.allocation_round(db,head,stage):
        payload['allocation_round']=round_[0]
    digest=fingerprint(payload);old=current(db,head,stage,_proofs=proofs,include_superseded=True);prior={r['sku']:r for r in old['payload']['rows']} if old else {}
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

def prepare(path, scanned, bindings, progress=lambda _: None, cancel=None, *, staged=None,_owned=False):
    """Bindings are exact whole-document heads, no per-line quantity allocations."""
    stage = scanned['stage']
    if stage not in ALL_STAGES: raise ValueError('未知生命周期环节')
    groups = {}; plans = []
    with closing(purchases.connect(path)) as db:
        checked=set()
        for row in scanned['rows']:
            checkpoint(cancel); head = bindings.get(binding_key(row))
            if not head or not matching(row, [head]): raise ValueError('单据未明确匹配，或门店/仓/单号/修订不一致')
            if head['warehouse'] not in purchases.wo.WAREHOUSES: raise ValueError('旧单仓归属不明确，不能猜测关联')
            token=tuple((k in head,head.get(k)) for k in ('oid','revision','warehouse','digest','current_revision','execution_epoch','visibility_token'))
            try:known=token in checked
            except TypeError:known=False;token=None
            if not known:
                purchases.validate_head(db, head)
                if token is not None:checked.add(token)
            groups.setdefault(key(head), {'head': head, 'rows': []})['rows'].append(row if _owned else copy.deepcopy(row))
        for i, group in enumerate(groups.values()):
            checkpoint(cancel); h = group['head']; unique = {}; duplicates = 0
            # Always recheck each source when freezing its plan. The immutable
            # binding cache above never replaces this or commit's fresh CAS.
            purchases.validate_head(db,h)
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
                row=store.file_row(db, f['sha'])
            if not row:raise ValueError('历史原件缺失')
            raw=row[0]
        else:raw = Path(f['path']).read_bytes()
        if hashlib.sha256(raw).hexdigest() != f['sha']: raise ValueError('原文件在预览后变化，请重新读取')
        contents.append((f, raw)); progress(RowProgress(f'留存原件 {i+1}/{len(plan["files"])}', i+1, len(plan['files'])))
    checkpoint(cancel)
    import storage_chunks
    storage_chunks.backup_before_write(Path(path), progress); checkpoint(cancel)
    from adaptive_resources import Budget, MIB
    from lifecycle_save_encoding import encode
    # Prepare validated TEXT one document at a time. The preview is owned by
    # this save; only external database state can change while it waits to write.
    memory_limit = max(1,min(16*MIB,Budget().cache_bytes))
    with tempfile.SpooledTemporaryFile(max_size=memory_limit, mode='w+b', dir=Path(path).parent) as encoded:
        positions = []
        count = len(plan['plans'])
        for i, item in enumerate(plan['plans'],1):
            checkpoint(cancel)
            progress(RowProgress(f'编码单据 {i}/{count}',i,count))
            offset = encoded.tell()
            _, digest = encode(item['payload'],retain=item['changed'],write=encoded.write)
            if digest != item['digest']:
                raise ValueError('预览内容变化')
            positions.append((offset,encoded.tell()-offset))

        deadline=time.monotonic()+_SAVE_BUSY_SECONDS
        while True:
            try:return _commit_once(path,plan,contents,encoded,positions,progress,cancel,deadline)
            except _BusyCommit as blocked:
                # Only a proven rollback permits retry, with all current
                # source/version/dependency checks repeated under the write lock.
                if time.monotonic()>=deadline:raise blocked.error
                checkpoint(cancel)
                progress(_RollbackWait('本地读取短暂占用，已安全回滚本次尝试；正在重新核验后保存，不会重复新增'))
                time.sleep(.025)


def _commit_once(path,plan,contents,encoded,positions,progress,cancel,deadline):
    """Own the transaction covering records, outbox, and payment mappings."""
    receipts = []
    with closing(sqlite3.connect(path, timeout=.15)) as db, db:
        progress('等待单据写锁…')
        begin(db, cancel,deadline=deadline)
        progress('已取得写锁，正在核对单据依据…')
        schema(db)
        if plan.get('jst_fulfillment') or any(r.get('adapter')=='jst_allocated' for p in plan['plans'] for r in p['payload']['raw_rows']):
            from jst_fulfillment import validate_guard
            validate_guard(db,plan)
        if plan.get('payment_matching'):
            payment_match_commit.validate(
                path, db, plan['payment_matching'], plan.get('payment_matching_digest'), plan['plans'],
            )
        staged={};replayed=set()
        for p in plan['plans']:
            verified=p.get('_read_proofs')
            checkpoint(cancel); purchases.validate_head(db, p['head'])
            identity=(*key(p['head']),p['stage'])
            if identity in staged:raise ValueError('同一订单同一环节只能保存一份完整单据')
            round_ = store.allocation_round(db,p['head'],p['stage'])
            if (round_ or (None,))[0] != p['payload'].get('allocation_round'):
                raise wr.Conflict('库存分配已重做，请按本次分配重新预览下游资料')
            if p['payload'].get('manual_correction'):
                target=staged_document(p)
                saved=store.version_row(db,p['head'],p['stage'],target['version'])
                if saved and saved[2]==target['digest']:
                    replayed.add(identity);staged[identity]=target;continue
                from order_amendments import validate
                validate(db,p)
            old = current(db, p['head'], p['stage'],_verified=verified,include_superseded=True)
            from special_settlement import group_for_oid
            if p['stage']=='receipt' and group_for_oid(db,p['head']['oid']):
                raise ValueError('开业组只登记一份原采购实收，请从新店开业工作台进入该张翱象采购')
            if p['stage'] in ('return','operations'):
                from material_stock import has_material, is_material
                if has_material(db) and is_material(db, p['head']['oid']):
                    raise ValueError('物料单请沿原申请更正，不能覆盖物料分配依据')
            # Saving store demand leaves the original execution and reservation in place.
            if p['stage']=='operations' and p['changed']:guard_operations(db,p['head'])
            if p['stage']=='payment':guard_payment(db,p)
            if p['stage']=='receipt' and p['payload'].get('aoxiang_payment_review'):
                from aoxiang_receipt_scope import validate_review
                validate_review(db,p['head'],p['payload']['aoxiang_payment_review'])
            if (old['version'] if old else None) != p['expected']: raise wr.Conflict('单据在预览后被更新，请重新核对')
            if p['payload']['basis'] != upstream(db,p['head'],p['stage'],staged,_verified=verified):raise wr.Conflict('上游单据在预览后变化，请重新核对')
            if p['changed'] != (not old or old['digest']!=p['digest']):raise ValueError('预览更新状态变化')
            staged[identity]=staged_document(p)
        # Last checkpoint. Never terminate the process between here and COMMIT.
        checkpoint(cancel); progress(COMMIT_BOUNDARY)
        for f, raw in contents: store.retain_file(db, f, raw)
        for p, (offset,length) in zip(plan['plans'], positions):
            v = p['expected'] or 0; h = p['head']
            identity=(*key(h),p['stage'])
            if identity in replayed:
                receipts.append(store.save_receipt(h,p['stage'],staged[identity]['version'],p['changed'],p['digest']))
                continue
            if p['changed']:
                encoded.seek(offset)
                data=encoded.read(length)
                if len(data)!=length:raise OSError('待保存的单据编码不完整，请重新核对')
                v += 1
                store.insert_version(db, h, p['stage'], v, p['digest'], data)
                del data
                if p['stage']==flow.demand_stage(db,h):
                    enqueue(db,h,{'version':v,'digest':p['digest']})
                store.insert_event(db, wr.now(), h, p['stage'], v, '确认完整单据', wr.dumps({'number': h['number'], 'rows': len(p['payload']['rows']), 'missing': p['payload']['missing'], 'extra': p['payload']['extra']}))
                if audit:=p['payload'].get('manual_correction'):
                    detail={k:audit[k] for k in ('id','operator','reason','prior','generation')}
                    store.insert_event(db,wr.now(),h,p['stage'],v,'手动更正',wr.dumps(detail))
            evidence=p.get('source_evidence')
            if evidence:
                if p['changed'] or any(f not in [{k:f[k] for k in ('name','sha')} for f,_ in contents] for f in evidence['files']):raise ValueError('原件留存范围与核对文件不一致')
                detail=wr.dumps(evidence);action='业务未变化，留存新原件'
                if not store.event_exists(db, h, p['stage'], v, action, detail):
                    store.insert_event(db, wr.now(), h, p['stage'], v, action, detail)
            receipts.append(store.save_receipt(h, p['stage'], v, p['changed'], p['digest']))
        if plan.get('payment_matching'):
            payment_match_commit.apply(db, plan['payment_matching'])
        try:db.commit()
        except sqlite3.OperationalError as exc:
            code=getattr(exc,'sqlite_errorcode',None)
            busy=(code & 255)==sqlite3.SQLITE_BUSY if isinstance(code,int) else str(exc).lower()=='database is locked'
            if not busy or not db.in_transaction:raise
            db.rollback()  # Failure here propagates: unknown outcomes never replay.
            raise _BusyCommit(exc) from exc
    return receipts

def _comparison_document(document):
    """Compact only after full decode/fingerprint verification; never persisted."""
    if document is None:return None
    payload=document['payload']
    rows=[{k:r[k] for k in ('sku','name','base_quantity','amount','currency','processing_note') if k in r} for r in payload['rows']]
    value={k:payload[k] for k in ('schema','pending') if k in payload}
    value['rows']=rows
    if 'basis' in payload:value['basis']={'dependencies':payload['basis'].get('dependencies',{})}
    return {**document,'payload':value}


def read_documents(db,head,cancel=None,*,as_of=None,compact=False):
    route=flow.stages(db,head,as_of=as_of)
    docs={}
    for stage in route:
        checkpoint(cancel)
        document=current(db,head,stage,as_of=as_of)
        docs[stage]=_comparison_document(document) if compact and stage!='payment' else document
        del document
    ordered=current(db,head,JST_ORDER,as_of=as_of)
    if ordered:
        docs[JST_ORDER]=_comparison_document(ordered) if compact else ordered
    return route,docs


def snapshot_state(head,route,docs,legacy,*,shipment=None):
    present={s:bool(docs[s]) for s in STAGES}
    if not present['return'] and legacy:present['return']=legacy['complete']
    if 'operations' in docs:present['operations']=bool(docs['operations'])
    status,next_stage,note,health=progress_state({s:document_summary(d) for s,d in docs.items()},legacy,route==flow.CURRENT,shipment=shipment)
    return {'head':head,'documents':docs,'legacy_return':legacy,'present':present,
            'status':status,'next':next_stage,'note':note,'health':health,'flow_version':2 if route==flow.CURRENT else 1}


def snapshot(path, head, progress=lambda _: None, cancel=None, db=None, *, order=None,as_of=None,_comparison=True):
    if db is None:
        with closing(purchases.connect(path)) as connection:
            connection.execute('BEGIN'); return snapshot(path, head, progress, cancel, connection, order=order,as_of=as_of,_comparison=_comparison)
    if order is None:
        from warehouse_execution import get_order
        order=get_order(repository(path),head,view='export')
    if order['digest'] != head['digest']: raise wr.Conflict('签发快照不一致')
    eligible = {d['E']: {'sku': d['E'], 'name': d.get('D'), 'quantity': sem.order_quantity(d)} for rs in sem.signed_groups(order['payload']['result'], order['payload'].get('new_focus')).values() for d in rs if ao.warehouse(d) == head['warehouse']}
    if not _comparison:del order  # The verified large signed tree is no longer needed.
    route,docs=read_documents(db,head,cancel,as_of=as_of,compact=not _comparison)
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
    shipment = read_shipments(db,[head['oid']],cancel).get(key(head)) if not as_of and docs['payment'] else None
    return {'rows':rows,**snapshot_state(head,route,docs,legacy,shipment=shipment)}

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
        row = store.file_row(db, sha)
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
        if kind in ('return', 'operations'):
            from lifecycle_exports import export_returns, retained_versions
            return export_returns(path, retained_versions(path, heads, kind, cancel), folder, mode, progress, cancel, stage=kind)
        views = []
        with closing(purchases.connect(path)) as db:
            db.execute('BEGIN')
            for h in heads:
                checkpoint(cancel); ao.require_active(db, h['oid'], h['warehouse']); view = snapshot(path, h, progress, cancel, db)
                if kind != 'comparison' and not view['documents'].get(kind): raise ValueError(h['number'] + '尚未导入' + title)
                views.append(view)
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
