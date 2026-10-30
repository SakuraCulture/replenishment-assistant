"""Rebuildable home projection, independent of model/signed snapshots."""
import json,sqlite3,hashlib
from contextlib import closing
from datetime import date,timedelta
from decimal import Decimal
from pathlib import Path
import work_repository as wr
import normal_exports,archive_orders as ao,warehouse_orders as wo,merchant_feedback as mf
from history_read import repository
from runtime_support import checkpoint
import purchases

def read(path,cache,request,cancel=None,progress=lambda _:None):
    start=request.get('from','');end=request.get('to','')
    for v in (start,end):
        if v:date.fromisoformat(v)
    if start and end and start>end:raise ValueError('起始日期不能晚于结束日期')
    repo=repository(path);heads=repo.headers();cache=Path(cache);cache.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(cache)) as idx,closing(sqlite3.connect(path)) as db:
        idx.execute('CREATE TABLE IF NOT EXISTS followup_projection(k TEXT PRIMARY KEY,payload TEXT,sha TEXT)')
        visibility=ao.states(db);has_purchase=purchases.exists(db)
        linkrows=db.execute('SELECT oid,revision,warehouse,sku,quantity FROM purchase_link_index').fetchall() if has_purchase else []
        purchase_values={};older=set()
        for oid,rev,wh,sku,qty in linkrows:
            key=(oid,rev,wh);purchase_values.setdefault(key,{})[sku]=purchase_values.get(key,{}).get(sku,Decimal(0))+Decimal(qty);older.add(key)
        has_reply=db.execute("SELECT 1 FROM sqlite_master WHERE name='merchant_feedback'").fetchone()
        old_replies={}
        if has_reply:
            for oid,rev,payload in db.execute('SELECT oid,revision,payload FROM merchant_feedback m WHERE version=(SELECT MAX(version) FROM merchant_feedback WHERE oid=m.oid AND revision=m.revision)'):
                p=wr.loads(payload);old_replies[(oid,rev)]=p
                if not p.get('warehouses') and p.get('values'):older.add((oid,rev,''))
                for wh,v in p.get('warehouses',{}).items():
                    if v.get('received'):older.add((oid,rev,wh))
        notes={}
        if has_purchase:
            notes={(oid,rev,wh):dict(wr.loads(p),token=token) for oid,rev,wh,token,p in db.execute('SELECT oid,revision,warehouse,token,payload FROM followup_notes')}
        selected=[h for h in heads if (not start or h['day']>=start) and (not end or h['day']<=end)]
        records=[]
        for n,h in enumerate(selected):
            checkpoint(cancel);k=wr.dumps([2,str(Path(path).resolve()),h['oid'],h['revision'],h['digest']]);hit=idx.execute('SELECT payload,sha FROM followup_projection WHERE k=?',(k,)).fetchone()
            if hit and hashlib.sha256(hit[0].encode()).hexdigest()==hit[1]:parts=json.loads(hit[0])
            else:
                o=repo.get(h['day'],h['store'],h['revision'],view='export');r=o['payload']['result'];parts=[]
                for wh,g in purchases.history_groups(r,o['payload'].get('new_focus')).items():
                    products=[d for ds in g.values() for d in ds];parts.append({'warehouse':wh,'name':r['store_name'],'quantities':{d['E']:normal_exports.sem.order_quantity(d) for d in products}})
                raw=json.dumps(parts,ensure_ascii=False);idx.execute('INSERT OR REPLACE INTO followup_projection VALUES(?,?,?)',(k,raw,hashlib.sha256(raw.encode()).hexdigest()));idx.commit()
            for part in parts:
                wh=part['warehouse'];state=visibility.get((h['oid'],wh),{})
                if state.get('deleted'):continue
                key=(h['oid'],h['revision'],wh);values=old_replies.get((h['oid'],h['revision']),{}).get('values',{});required=[s for s,q in part['quantities'].items() if q>0];missing=[s for s in required if s not in values];received=sum(s in values for s in required)
                status='无应确认项' if not required else '回传齐全' if not missing else '部分回传' if received else '待回传'
                actual=purchase_values.get(key,{});gaps=[s for s in required if s not in actual]
                purchase='无应确认项' if not required and not actual else '尚未关联下单资料' if not actual else '部分关联' if gaps else '已关联（需核对范围）'
                changed={s for s,q in actual.items() if s in values and q!=Decimal(str(values[s]))}
                changed.update(s for s,q in part['quantities'].items() if s in values and Decimal(str(values[s]))!=Decimal(str(q)))
                diff=len(changed)
                old=any(oid==h['oid'] and rev!=h['revision'] and w in (wh,'') for oid,rev,w in older)
                note=notes.get(key,{});deferred=note.get('action')=='暂缓跟进' and (not note.get('until') or note['until']>wr.today())
                observed=purchases.observation(db,h);acknowledged=note.get('action')=='已核实保留差异' and note.get('observation')==observed
                issues=[]
                if wh==ao.LEGACY:issues.append('旧单仓归属待核')
                if missing:issues.append(status)
                if required and purchase!='已关联（需核对范围）':issues.append(purchase)
                if old and not acknowledged:issues.append('旧修订待核')
                if diff and not acknowledged:issues.append('差异待核')
                record={**h,**{x:part[x] for x in ('warehouse','name')},'number':ao.number(h['number'],wh),'parent_number':h['number'],'current_revision':h['revision'],'visibility_token':state.get('token',0),'reply_status':status,'pending':len(missing),'purchase_status':purchase,'differences':diff,'old_revision':old,'issues':issues,'deferred':deferred,'note':note,'wait_days':max(0,(date.fromisoformat(wr.today())-date.fromisoformat(h['saved'][:10])).days)}
                record.update(observation=observed,acknowledged=acknowledged,note_token=note.get('token'))
                query=request.get('search','').strip().lower()
                if all(t in wr.dumps(record).lower() for t in query.split()):records.append(record)
            progress({'done':n+1,'total':len(selected)})
        if heads!=repo.headers() or visibility!=ao.states(db):raise wr.Conflict('归档在查询期间变化，请刷新')
    counts={s:sum(s in r['issues'] for r in records if not r['deferred']) for s in ('待回传','部分回传','尚未关联下单资料','部分关联','差异待核','旧修订待核')}
    for i,r in enumerate(records):r['list_position']=i
    kind=request.get('status','')
    matched=[r for r in records if (not kind or kind=='全部未完成' and r['issues'] and not r['deferred'] or kind=='暂缓跟进' and r['deferred'] or kind in r['issues'] and not r['deferred'])]
    page=max(0,int(request.get('page',0)));page=min(page,max(0,(len(matched)-1)//50))
    return {'records':matched[page*50:(page+1)*50],'count':len(matched),'page':page,'counts':counts,'scope':len(records)}

def note(path,head,action,reason,until='',progress=lambda _:None,cancel=None):
    if action not in ('暂缓跟进','恢复跟进','已核实保留差异'):raise ValueError('未知跟进操作')
    if not reason.strip():raise ValueError('请填写原因')
    if until:date.fromisoformat(until)
    checkpoint(cancel)
    import storage_chunks
    storage_chunks.backup_before_write(Path(path),progress)
    with closing(sqlite3.connect(path)) as db,db:
        db.execute('BEGIN IMMEDIATE');checkpoint(cancel);purchases.schema(db);purchases.validate_head(db,head)
        previous=db.execute('SELECT token FROM followup_notes WHERE oid=? AND revision=? AND warehouse=?',(head['oid'],head['revision'],head['warehouse'])).fetchone()
        if (previous[0] if previous else None)!=head.get('note_token'):raise wr.Conflict('跟进记录已变化，请刷新')
        observed=purchases.observation(db,head)
        if observed!=head.get('observation'):raise wr.Conflict('下单/回传资料已变化，请重新核对')
        import uuid
        payload={'action':action,'reason':reason,'until':until,'at':wr.now(),'observation':observed}
        checkpoint(cancel)
        db.execute('INSERT INTO followup_notes VALUES(?,?,?,?,?) ON CONFLICT(oid,revision,warehouse) DO UPDATE SET token=excluded.token,payload=excluded.payload',(head['oid'],head['revision'],head['warehouse'],uuid.uuid4().hex,wr.dumps(payload)))
        db.execute('INSERT INTO purchase_events(at,action,detail) VALUES(?,?,?)',(wr.now(),action,wr.dumps({'head':head,'note':payload})))
