"""Document-derived home progress. No manual follow-up flags or partial stages."""
import hashlib, json, sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path
import work_repository as wr
import purchases, archive_orders as ao, normal_exports
import order_lifecycle as lifecycle
from history_read import repository
from runtime_support import checkpoint

def read(path, cache, request, cancel=None, progress=lambda _: None):
    start = request.get('from', ''); end = request.get('to', '')
    for value in (start, end):
        if value: date.fromisoformat(value)
    if start and end and start > end: raise ValueError('起始日期不能晚于结束日期')
    repo = repository(path); heads = repo.headers(); cache = Path(cache); cache.parent.mkdir(parents=True, exist_ok=True)
    records = []
    with closing(sqlite3.connect(cache)) as idx, closing(purchases.connect(path)) as db:
        db.execute('BEGIN'); visibility = ao.states(db)
        idx.execute('CREATE TABLE IF NOT EXISTS lifecycle_projection(k TEXT PRIMARY KEY,payload TEXT,sha TEXT)')
        versions = {}
        if lifecycle.exists(db):
            versions = {(o, r, w, s): v for o, r, w, s, v in db.execute('SELECT oid,revision,warehouse,stage,MAX(version) FROM lifecycle_versions GROUP BY oid,revision,warehouse,stage')}
        selected = [h for h in heads if (not start or h['day'] >= start) and (not end or h['day'] <= end)]
        for n, h in enumerate(selected):
            checkpoint(cancel); k = wr.dumps([1, str(Path(path).resolve()), h['oid'], h['revision'], h['digest']])
            hit = idx.execute('SELECT payload,sha FROM lifecycle_projection WHERE k=?', (k,)).fetchone()
            if hit and hashlib.sha256(hit[0].encode()).hexdigest() == hit[1]: parts = json.loads(hit[0])
            else:
                o = repo.get(h['day'], h['store'], h['revision'], view='export'); result = o['payload']['result']; parts = []
                for wh, group in purchases.history_groups(result, o['payload'].get('new_focus')).items():
                    parts.append({'warehouse': wh, 'name': result['store_name'], 'products': {d['E']: {'quantity': normal_exports.sem.order_quantity(d)} for rows in group.values() for d in rows}})
                raw = json.dumps(parts, ensure_ascii=False); idx.execute('INSERT OR REPLACE INTO lifecycle_projection VALUES(?,?,?)', (k, raw, hashlib.sha256(raw.encode()).hexdigest())); idx.commit()
            for part in parts:
                wh = part['warehouse']; vis = visibility.get((h['oid'], wh), {})
                if vis.get('deleted'): continue
                head = {**h, 'warehouse': wh}; present = {s: (h['oid'], h['revision'], wh, s) in versions for s in lifecycle.STAGES}
                legacy = lifecycle.legacy_return(db, head, part['products']) if not present['return'] else None
                if legacy: present['return'] = legacy['complete']
                summaries={}
                for s in lifecycle.STAGES:
                    version=versions.get((h['oid'],h['revision'],wh,s))
                    if version is None:summaries[s]=None;continue
                    doc_key=wr.dumps(['lifecycle-v2',str(Path(path).resolve()),h['oid'],h['revision'],wh,s,version])
                    cached=idx.execute('SELECT payload,sha FROM lifecycle_projection WHERE k=?',(doc_key,)).fetchone()
                    if cached and hashlib.sha256(cached[0].encode()).hexdigest()==cached[1]:summary=json.loads(cached[0])
                    else:
                        summary=lifecycle.document_summary(lifecycle.current(db,head,s,version));text=json.dumps(summary,ensure_ascii=False)
                        idx.execute('INSERT OR REPLACE INTO lifecycle_projection VALUES(?,?,?)',(doc_key,text,hashlib.sha256(text.encode()).hexdigest()));idx.commit()
                    summaries[s]=summary
                status,next_stage,note,health=lifecycle.progress_state(summaries,legacy)
                old = any(o == h['oid'] and r != h['revision'] and w == wh for o, r, w, s in versions)
                record = {**h, 'warehouse': wh, 'name': part['name'], 'number': ao.number(h['number'], wh), 'parent_number': h['number'], 'current_revision': h['revision'], 'visibility_token': vis.get('token', 0), 'present': present, 'status': status, 'next': next_stage, 'old_revision': old, 'legacy_return': bool(legacy), 'legacy_incomplete': bool(legacy and not legacy['complete']), 'wait_days': max(0, (date.fromisoformat(wr.today()) - date.fromisoformat(h['saved'][:10])).days)}
                record.update(note=note,health=health)
                query = request.get('search', '').strip().lower()
                search = ' '.join(str(record[k]) for k in ('day', 'store', 'name', 'warehouse', 'number')).lower()
                if all(t in search for t in query.split()): records.append(record)
            progress({'done': n+1, 'total': len(selected)})
        if heads != repo.headers(): raise wr.Conflict('签发修订在查询期间变化，请刷新')
    counts = {s: sum(r['status'] == s for r in records) for s in lifecycle.STATUSES}
    for i, r in enumerate(records): r['list_position'] = i
    kind = request.get('status', '')
    matched = [r for r in records if not kind or kind == '全部未完成' and r['next'] or kind == r['status']]
    page = min(max(0, int(request.get('page', 0))), max(0, (len(matched)-1)//50))
    return {'records': matched[page*50:(page+1)*50], 'count': len(matched), 'page': page, 'counts': counts, 'scope': len(records)}
