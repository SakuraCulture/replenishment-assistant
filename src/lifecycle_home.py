"""Document-derived home progress. No manual follow-up flags or partial stages."""
import hashlib, json, sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path
import work_repository as wr
import archive_orders as ao
import history_snapshot
from lifecycle_facts import current, legacy_return
from app_defaults import WAREHOUSES
from result_semantics import SIGNED_GROUPS, order_quantity, signed_rows
from lifecycle_rules import JST_ORDER, basis_token, document_summary
from lifecycle_stages import CURRENT as ALL_STAGES, STATUSES, progress_state
from jst_shipping import read as read_shipments
from order_keys import warehouse_revision
import warehouse_execution as execution
from normal_orders import read_repository as repository
from runtime_support import checkpoint
from query_scope import oid_batches


DOCUMENT_STAGES = (*ALL_STAGES, JST_ORDER)


def _signed_parts(profile, rows, needs_legacy):
    found = {}
    for group, row in signed_rows(rows, profile):
        warehouse = ao.warehouse(row)
        groups = found.setdefault(warehouse, {})
        if needs_legacy:
            groups.setdefault(group, {})[row['E']] = {'quantity': order_quantity(row)}
    parts = []
    for warehouse in (*WAREHOUSES, ao.LEGACY):
        if warehouse not in found:
            continue
        part = {'warehouse': warehouse}
        if needs_legacy:
            products = {}
            for group in SIGNED_GROUPS:
                products.update(found[warehouse].get(group, {}))
            part['products'] = products
        parts.append(part)
    return parts


def read(path, cache, request, cancel=None, progress=lambda _: None):
    start = request.get('from', ''); end = request.get('to', '')
    for value in (start, end):
        if value: date.fromisoformat(value)
    if start and end and start > end: raise ValueError('起始日期不能晚于结束日期')
    repo = repository(path); heads = repo.headers(); cache = Path(cache); cache.parent.mkdir(parents=True, exist_ok=True)
    records = [];basis_tokens={};source=str(Path(path).resolve())
    track_platform=request.get('platform_tracking',False)
    if track_platform:import order_tracking_refs
    terms=request.get('search','').strip().lower().split()
    selected = [h for h in heads if (not start or h['day'] >= start) and (not end or h['day'] <= end)]
    selected_parents=list(selected)
    with closing(sqlite3.connect(cache)) as idx, closing(wr.read_connection(path, timeout=10)) as db:
        db.execute('BEGIN'); visibility = ao.states(db)
        managed=execution.managed(db,(h['oid'] for h in selected))
        managed_heads=execution.heads(db,[h for h in selected if h['oid'] in managed]) if managed else []
        by_parent={}
        for h in managed_heads:by_parent.setdefault(h['oid'],[]).append(h)
        selected=[child for h in selected for child in (by_parent.get(h['oid'],[]) if h['oid'] in managed else [h])]
        idx.execute('CREATE TABLE IF NOT EXISTS lifecycle_projection(k TEXT PRIMARY KEY,payload TEXT,sha TEXT)')
        versions = {}
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        routes={};legacy_keys=set()
        for placeholders,oids in oid_batches(h['oid'] for h in selected):
            if 'lifecycle_routes' in tables:routes.update(((o,r),v) for o,r,v in db.execute('SELECT oid,revision,version FROM lifecycle_routes WHERE oid IN ('+placeholders+')',oids))
            if 'merchant_feedback' in tables:legacy_keys.update(db.execute('SELECT DISTINCT oid,revision FROM merchant_feedback WHERE oid IN ('+placeholders+')',oids))
        if 'lifecycle_versions' in tables:
            for placeholders,oids in oid_batches(h['oid'] for h in selected):
                checkpoint(cancel)
                versions.update(((o,r,w,s),v) for o,r,w,s,v in db.execute('SELECT oid,revision,warehouse,stage,MAX(version) FROM lifecycle_versions WHERE oid IN ('+placeholders+') GROUP BY oid,revision,warehouse,stage',oids))
        shipments = read_shipments(db,(o for o,r,w,s in versions if s=='payment'),cancel)
        older=set()
        selected_revisions={(h['oid'],h.get('warehouse')):h['revision'] for h in selected}
        active_generations={}
        if 'jst_active' in tables:
            for marks,oids in oid_batches(o for o,r,w,s in versions if s==JST_ORDER):
                active_generations.update(((o,w),gid) for o,w,gid in db.execute(
                    'SELECT oid,warehouse,gid FROM jst_active WHERE oid IN ('+marks+')',oids))

        def document_key(identity):
            oid, _, warehouse, stage = identity
            prefix='lifecycle-v3' if track_platform and stage=='payment' else 'lifecycle-v2'
            parts=[prefix,source,*identity,versions[identity]]
            if stage==JST_ORDER:
                parts.append(active_generations.get((oid,warehouse)))
            return wr.dumps(parts)

        doc_keys={}
        for oid,revision,warehouse,stage in versions:
            if revision!=selected_revisions.get((oid,warehouse),selected_revisions.get((oid,None))):older.add((oid,warehouse))
            elif stage in DOCUMENT_STAGES:
                key=(oid,revision,warehouse,stage)
                doc_keys[key]=document_key(key)
        cached_documents={}
        for placeholders,keys in oid_batches(doc_keys.values()):
            checkpoint(cancel)
            cached_documents.update((key,(payload,sha)) for key,payload,sha in idx.execute(
                'SELECT k,payload,sha FROM lifecycle_projection WHERE k IN ('+placeholders+')',keys))
        for n, h in enumerate(selected):
            needs_legacy=(h['oid'],h['revision']) in legacy_keys
            checkpoint(cancel); cache_parts=[2,source,h['oid'],h['revision'],h['digest'],needs_legacy]
            if h.get('warehouse'):cache_parts.extend([h['warehouse'],h.get('snapshot_digest'),h.get('execution_epoch')])
            k=wr.dumps(cache_parts)
            hit = idx.execute('SELECT payload,sha FROM lifecycle_projection WHERE k=?', (k,)).fetchone()
            if hit and hashlib.sha256(hit[0].encode()).hexdigest() == hit[1]: parts = json.loads(hit[0])
            else:
                result, _, parts = history_snapshot.read(
                    db, h, lambda profile, rows: _signed_parts(profile, rows, needs_legacy),
                    managed=bool(h.get('warehouse')), cancel=cancel,
                )
                for part in parts:
                    part['name'] = result['store_name']
                raw = json.dumps(parts, ensure_ascii=False); idx.execute('INSERT OR REPLACE INTO lifecycle_projection VALUES(?,?,?)', (k, raw, hashlib.sha256(raw.encode()).hexdigest()))
            for part in parts:
                wh = part['warehouse']; vis = visibility.get((h['oid'], wh), {})
                if vis.get('deleted'): continue
                head = {**h, 'warehouse': wh}; present = {s: (h['oid'], h['revision'], wh, s) in versions for s in ALL_STAGES}
                legacy = legacy_return(db, head, part['products']) if not present['return'] and (h['oid'],h['revision']) in legacy_keys else None
                if legacy: present['return'] = legacy['complete']
                summaries={}
                for s in DOCUMENT_STAGES:
                    version=versions.get((h['oid'],h['revision'],wh,s))
                    if version is None:summaries[s]=None;continue
                    with_number=track_platform and s=='payment'
                    identity=(*warehouse_revision(head),s)
                    doc_key=doc_keys.get(identity) or document_key(identity)
                    cached=cached_documents.get(doc_key)
                    if cached and hashlib.sha256(cached[0].encode()).hexdigest()==cached[1]:summary=json.loads(cached[0])
                    else:
                        document=current(db,head,s,version);summary=document_summary(document)
                        if with_number:summary['jst_numbers']=order_tracking_refs.payment_numbers(document)
                        del document
                        text=json.dumps(summary,ensure_ascii=False)
                        idx.execute('INSERT OR REPLACE INTO lifecycle_projection VALUES(?,?,?)',(doc_key,text,hashlib.sha256(text.encode()).hexdigest()))
                    summaries[s]=summary
                required=routes.get((h['oid'],h['revision']),0)>=2 or present['operations']
                if request.get('_with_tokens'):basis_tokens[h['oid'],wh]={s:basis_token(d) for s,d in summaries.items()}
                shipment = shipments.get(warehouse_revision(head))
                status,next_stage,note,health=progress_state(summaries,legacy,required,shipment=shipment)
                old = (h['oid'],wh) in older
                parent_number=h.get('parent_number',h['number'])
                record = {**h, 'warehouse': wh, 'name': part['name'], 'number': ao.number(parent_number, wh), 'parent_number': parent_number, 'current_revision': h.get('current_revision',h['revision']), 'visibility_token': vis.get('token', 0), 'present': present, 'status': status, 'next': next_stage, 'old_revision': old, 'legacy_return': bool(legacy), 'legacy_incomplete': bool(legacy and not legacy['complete']), 'wait_days': max(0, (date.fromisoformat(wr.today()) - date.fromisoformat(h['saved'][:10])).days)}
                record.update(note=note,health=health,flow_version=2 if required else 1,shipment=shipment)
                search = ' '.join(str(record[k]) for k in ('day', 'store', 'name', 'warehouse', 'number')).lower()
                if track_platform:
                    payment=summaries['payment'] or {}
                    record['platform_tracking']={'jst_numbers':payment.get('jst_numbers',[]),
                        'payment_basis':(payment.get('version'),payment.get('digest')),
                        'payment_positive':payment.get('positive',0),'receipt_saved':bool(summaries['receipt'])}
                    records.append(record)
                elif all(t in search for t in terms):records.append(record)
            progress({'done': n+1, 'total': len(selected)})
        if track_platform:
            order_tracking_refs.attach(path,db,records,cancel)
            if terms:records=[r for r in records if order_tracking_refs.matches(r,terms)]
        if heads != repo.headers(): raise wr.Conflict('签发修订在查询期间变化，请刷新')
        with closing(wr.read_connection(path, timeout=10)) as fresh:
            now_managed=execution.managed(fresh,(h['oid'] for h in selected_parents))
            if now_managed!=managed or (managed and execution.heads(fresh,[h for h in selected_parents if h['oid'] in managed])!=managed_heads):raise wr.Conflict('仓单签发范围在查询期间变化，请刷新')
        idx.commit()
    counts = {s: sum(r['status'] == s for r in records) for s in (*STATUSES,'待运营签发')}
    for i, r in enumerate(records): r['list_position'] = i
    if request.get('all_records'):return {'records':records,'counts':counts,'scope':len(records),**({'_basis_tokens':basis_tokens} if request.get('_with_tokens') else {})}
    kind = request.get('status', '')
    matched = [r for r in records if not kind or kind == '全部未完成' and r['next'] or kind == r['status']]
    page = min(max(0, int(request.get('page', 0))), max(0, (len(matched)-1)//50))
    return {'records': matched[page*50:(page+1)*50], 'count': len(matched), 'page': page, 'counts': counts, 'scope': len(records)}
