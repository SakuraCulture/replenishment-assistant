"""Independent purchase facts/versions/allocations. Never writes signed model tables."""
import copy,hashlib,json,sqlite3,uuid,os,tempfile
from pathlib import Path
from contextlib import closing
from decimal import Decimal
import work_repository as wr
import normal_exports,merchant_feedback,archive_orders as ao,warehouse_orders as wo
from history_read import repository
from runtime_support import checkpoint
from purchase_io import number
from export_progress import RowProgress
SCHEMA=1

def connect(path):return sqlite3.connect(path,timeout=10)
def exists(db):return db.execute("SELECT 1 FROM sqlite_master WHERE name='purchase_documents'").fetchone() is not None
def schema(db):
    for sql in [
        'CREATE TABLE IF NOT EXISTS purchase_documents(docid TEXT PRIMARY KEY,identity TEXT NOT NULL UNIQUE,current INTEGER NOT NULL)',
        'CREATE TABLE IF NOT EXISTS purchase_versions(docid TEXT NOT NULL,version INTEGER NOT NULL,saved TEXT NOT NULL,digest TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(docid,version))',
        'CREATE TABLE IF NOT EXISTS purchase_files(sha TEXT PRIMARY KEY,name TEXT NOT NULL,content BLOB NOT NULL)',
        'CREATE TABLE IF NOT EXISTS purchase_events(id INTEGER PRIMARY KEY,at TEXT NOT NULL,docid TEXT,version INTEGER,action TEXT NOT NULL,detail TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS purchase_mappings(name TEXT PRIMARY KEY,payload TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS purchase_link_index(docid TEXT,lineid TEXT,oid TEXT,revision INTEGER,warehouse TEXT,sku TEXT,quantity TEXT,PRIMARY KEY(docid,lineid,oid,revision,warehouse))',
        'CREATE TABLE IF NOT EXISTS followup_notes(oid TEXT,revision INTEGER,warehouse TEXT,token TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(oid,revision,warehouse))',
        'CREATE INDEX IF NOT EXISTS purchase_link_order ON purchase_link_index(oid,revision,warehouse,sku)']:
        db.execute(sql)

def identity(row):return [row[k] for k in ('system','number','store','warehouse')]
def docid(row):return hashlib.sha256(wr.dumps(identity(row)).encode()).hexdigest()
def fact(row):return {k:v for k,v in row.items() if k not in ('source','raw','sources','external_line_id','identity_reviewed')}
def fingerprint(payload):
    # Full retained business/audit values, including raw cells and line identity.
    # The transient preview approval and local file location are not fact identity.
    values={'schema':payload['schema'],'rows':[{k:v for k,v in r.items() if k!='identity_reviewed'} for r in payload['rows']],'files':[{k:f[k] for k in ('sha','name')} for f in payload['files']]}
    return hashlib.sha256(wr.dumps(values).encode()).hexdigest()
def current(db,did,version=None):
    if not exists(db):return None
    row=db.execute('SELECT d.current,v.version,v.saved,v.digest,v.payload FROM purchase_documents d JOIN purchase_versions v ON d.docid=v.docid WHERE d.docid=? AND v.version=COALESCE(?,d.current)',(did,version)).fetchone()
    if not row:return None
    payload=wr.loads(row[4])
    if payload.get('schema')!=SCHEMA:raise ValueError('不支持的采购资料版本，请使用对应版本程序')
    if fingerprint(payload)!=row[3]:raise ValueError('采购快照校验失败')
    return {'docid':did,'current':row[0],'version':row[1],'saved':row[2],'digest':row[3],'payload':payload}

def documents(path,progress=lambda _:None,cancel=None):
    with closing(connect(path)) as db:
        if not exists(db):return []
        ids=[r[0] for r in db.execute('SELECT docid FROM purchase_documents ORDER BY rowid DESC')]
        out=[]
        for i,did in enumerate(ids):checkpoint(cancel);out.append(current(db,did));progress(RowProgress(f'采购资料 {i+1}/{len(ids)}',i+1,len(ids)))
        return out

def history_groups(result,profile):
    """Preserve old unlabelled warehouse rows as review-only, never guess a route."""
    grouped=normal_exports.groups(result,profile);out={}
    for wh in (*wo.WAREHOUSES,ao.LEGACY):
        subset={sheet:[d for d in rows if ao.warehouse(d)==wh] for sheet,rows in grouped.items()}
        if any(subset.values()):out[wh]=subset
    return out

def targets(path,scopes=None,progress=lambda _:None,cancel=None):
    repo=repository(path);out=[]
    with closing(connect(path)) as db:
        visibility=ao.states(db)
        headers=db.execute('SELECT o.day,o.store,o.oid,o.number,o.current,r.revision,r.digest,r.saved FROM normal_orders o JOIN normal_revisions r ON o.oid=r.oid ORDER BY o.day DESC,o.store,r.revision DESC').fetchall()
    if scopes:headers=[h for h in headers if any(s==h[1] for s,w in scopes)]
    for i,(day,store,oid,parent,cur,rev,digest,saved) in enumerate(headers):
        checkpoint(cancel);order=repo.get(day,store,rev,view='export');result=order['payload']['result']
        for wh,groups in history_groups(result,order['payload'].get('new_focus')).items():
            if wh not in wo.WAREHOUSES:continue
            if scopes and (store,wh) not in scopes:continue
            if visibility.get((oid,wh),{}).get('deleted'):continue
            rows=[r for rs in groups.values() for r in rs]
            out.append({'oid':oid,'day':day,'store':store,'name':result['store_name'],'number':wo.child_number(parent,wh),'parent_number':parent,'warehouse':wh,'revision':rev,'current_revision':cur,'digest':digest,'saved':saved,'visibility_token':visibility.get((oid,wh),{}).get('token',0),'products':{d['E']:{'sku':d['E'],'name':d.get('D'),'quantity':normal_exports.sem.order_quantity(d),'unit':d.get('G'),'price':d.get('V')} for d in rows}})
        progress(RowProgress(f'读取可关联修订 {i+1}/{len(headers)}',i+1,len(headers)))
    return out

def link(head,qty,extra=False):
    return {'head':{k:v for k,v in head.items() if k!='products'},'quantity':number(qty),'extra':bool(extra)}

def auto_links(scanned,available):
    out=copy.deepcopy(scanned)
    for r in out['rows']:
        if r.get('links') or r['base_quantity'] is None or r['status']=='已取消':continue
        candidates=[h for h in available if h['number']==r['signed_number'] and h['revision']==r['signed_revision'] and h['store']==r['store'] and h['warehouse']==r['warehouse'] and r['sku'] in h['products']]
        if len(candidates)==1:r['links']=[link(candidates[0],r['base_quantity'])]
    return out

def retain_links(scan,docs):
    """Exact source replay retains human-reviewed conversion/identity/links."""
    current_docs={d['docid']:d for d in docs};out=copy.deepcopy(scan)
    for r in out['rows']:
        old=current_docs.get(docid(r));match=next((x for x in old['payload']['rows'] if x['line_id']==r['line_id']),None) if old else None
        if not match and old:match=next((x for x in old['payload']['rows'] if x['source']==r['source'] and x['raw']==r['raw']),None)
        if match and match['source']==r['source'] and match['raw']==r['raw']:
            r.clear();r.update(copy.deepcopy(match));r['identity_reviewed']=True;continue
        if match and {k:v for k,v in fact(r).items() if k!='links'}=={k:v for k,v in fact(match).items() if k!='links'}:
            r['links']=copy.deepcopy(match['links']);r['identity_reviewed']=True
    return out

def validate_head(db,h):
    ao.require_active(db,h['oid'],h['warehouse'])
    row=db.execute('SELECT o.current,r.digest FROM normal_orders o JOIN normal_revisions r ON o.oid=r.oid WHERE o.oid=? AND r.revision=?',(h['oid'],h['revision'])).fetchone()
    if not row or row!=(h['current_revision'],h['digest']):raise wr.Conflict('签发修订已更新，请重新读取并核对关联')
    if ao.states(db).get((h['oid'],h['warehouse']),{}).get('token',0)!=h.get('visibility_token',0):raise wr.Conflict('归档可见性已变化，请重新核对')

def prepare(path,scanned,progress=lambda _:None,cancel=None):
    checkpoint(cancel);groups={};repo=repository(path);signed={}
    for row in copy.deepcopy(scanned['rows']):
        did=docid(row);groups.setdefault(did,[]).append(row)
    plans=[]
    with closing(connect(path)) as db:
        for index,(did,items) in enumerate(groups.items()):
            checkpoint(cancel);old=current(db,did);unique={};duplicates=0
            for r in items:
                if r['line_id'] in unique:
                    if wr.dumps(fact(r))!=wr.dumps(fact(unique[r['line_id']])):raise ValueError('同一采购明细号内容冲突：'+r['line_id'])
                    unique[r['line_id']].setdefault('sources',[unique[r['line_id']]['source']]).append(r['source']);duplicates+=1;continue
                unique[r['line_id']]=r
            rows=list(unique.values())
            if len({r['day'] for r in rows})!=1 or len({r['status'] for r in rows})!=1:raise ValueError('同一采购单的日期/状态不一致')
            for r in rows:
                if r['links'] and (r['base_quantity'] is None or r['status']=='已取消'):raise ValueError('单位待核或已取消采购行不能关联；请先撤销关联')
                seen=set();total=Decimal(0)
                for l in r['links']:
                    h=l['head'];k=(h['oid'],h['revision'],h['warehouse'])
                    if k in seen:raise ValueError('同一采购行不能重复关联同一签发修订')
                    seen.add(k);validate_head(db,h)
                    if h['store']!=r['store'] or h['warehouse']!=r['warehouse']:raise ValueError('禁止跨门店/仓库关联')
                    if k not in signed:
                        o=repo.get(h['day'],h['store'],h['revision'],view='export')
                        signed[k]={d['E']:d for ds in normal_exports.groups(o['payload']['result'],o['payload'].get('new_focus')).values() for d in ds if d.get('AU')==h['warehouse']}
                    if r['sku'] not in signed[k] and not l.get('extra'):raise ValueError('商品不在此仓签发表；需明确选择额外采购')
                    l['quantity']=number(l['quantity']);total+=Decimal(l['quantity'])
                    reply=merchant_feedback.current(db,h['oid'],h['revision']);l['feedback_version']=reply['version'] if reply else None
                if r['base_quantity'] is not None and total>Decimal(r['base_quantity']):raise ValueError('分摊量超过本版实际下单量：'+r['sku'])
            used={s['sha'] for r in rows for s in [r['source'],*r.get('sources',[])]}
            payload={'schema':SCHEMA,'rows':rows,'files':[f for f in scanned.get('files',[]) if f['sha'] in used]};digest=fingerprint(payload)
            changed=old is None or digest!=old['digest']
            if changed and old and any(not r.get('external_line_id') and not r.get('identity_reviewed') for r in rows):raise ValueError('无采购明细号的更新，请逐行确认旧行映射或明确新增行')
            before={r['line_id']:r for r in old['payload']['rows']} if old else {};after={r['line_id']:r for r in rows};differences=[]
            for lid in list(before)+[k for k in after if k not in before]:
                a,b=before.get(lid),after.get(lid)
                if a is None or b is None or wr.dumps(fact(a))!=wr.dumps(fact(b)) or a['source']!=b['source'] or a.get('sources',[])!=b.get('sources',[]):
                    differences.append({'line':lid,'sku':(b or a)['sku'],'name':(b or a)['name'],'before':a,'after':b})
            plans.append({'docid':did,'identity':identity(rows[0]),'expected':old['version'] if old else None,'digest':digest,'payload':payload,'changed':changed,'differences':differences,'duplicates':duplicates})
            progress(RowProgress(f'核对采购单 {index+1}/{len(groups)}：'+rows[0]['number'],index+1,len(groups)))
    return {'plans':plans,'files':scanned.get('files',[])}

def commit(path,plan,progress=lambda _:None,cancel=None):
    checkpoint(cancel);contents=[]
    for f in plan['files']:
        checkpoint(cancel);data=Path(f['path']).read_bytes()
        if hashlib.sha256(data).hexdigest()!=f['sha']:raise ValueError('文件在预览后变化，请重新读取')
        contents.append((f,data))
    encoded=[wr.dumps(p['payload']) for p in plan['plans']];checkpoint(cancel)
    import storage_chunks
    storage_chunks.backup_before_write(Path(path),progress);checkpoint(cancel)
    receipts=[]
    with closing(connect(path)) as db,db:
        db.execute('BEGIN IMMEDIATE');checkpoint(cancel);schema(db)
        for p in plan['plans']:
            old=current(db,p['docid'])
            if (old['version'] if old else None)!=p['expected']:raise wr.Conflict('采购记录已被另一窗口更新，请重新预览')
            if fingerprint(p['payload'])!=p['digest']:raise ValueError('预览内容发生变化')
            for row in p['payload']['rows']:
                for l in row['links']:
                    h=l['head'];validate_head(db,h);reply=merchant_feedback.current(db,h['oid'],h['revision'])
                    if (reply['version'] if reply else None)!=l['feedback_version']:raise wr.Conflict('回传已更新，请重新核对')
        progress('即将提交采购资料；此后提交完成前不可中断');checkpoint(cancel)
        # Final short atomic boundary: no cancellation/termination inside writes.
        for f,data in contents:db.execute('INSERT OR IGNORE INTO purchase_files VALUES(?,?,?)',(f['sha'],f['name'],data))
        for p,raw in zip(plan['plans'],encoded):
            version=p['expected'] or 0
            if p['changed']:
                version+=1;db.execute('INSERT INTO purchase_documents VALUES(?,?,?) ON CONFLICT(docid) DO UPDATE SET current=excluded.current',(p['docid'],wr.dumps(p['identity']),version))
                db.execute('INSERT INTO purchase_versions VALUES(?,?,?,?,?)',(p['docid'],version,wr.now(),p['digest'],raw))
                db.execute('DELETE FROM purchase_link_index WHERE docid=?',(p['docid'],))
                for r in p['payload']['rows']:
                    for l in r['links']:
                        h=l['head'];db.execute('INSERT INTO purchase_link_index VALUES(?,?,?,?,?,?,?)',(p['docid'],r['line_id'],h['oid'],h['revision'],h['warehouse'],r['sku'],l['quantity']))
            db.execute('INSERT INTO purchase_events(at,docid,version,action,detail) VALUES(?,?,?,?,?)',(wr.now(),p['docid'],version,'保存采购资料' if p['changed'] else '重复导入未增加数量',wr.dumps({'files':plan['files'],'changes':len(p['differences'])})))
            receipts.append({'docid':p['docid'],'number':p['identity'][1],'version':version,'changed':p['changed'],'unlinked':sum(not r['links'] for r in p['payload']['rows'])})
    return receipts

def revise(path,did,version,rows,progress=lambda _:None,cancel=None):
    with closing(connect(path)) as db:old=current(db,did)
    if not old or old['version']!=version:raise wr.Conflict('采购版本已变化，请刷新')
    # Archived sources need not still exist on disk. New relation version keeps refs.
    scan={'rows':rows,'files':[]};plan=prepare(path,scan,progress,cancel)
    if len(plan['plans'])!=1 or plan['plans'][0]['docid']!=did:raise ValueError('关联修改不能变更外部单身份')
    p=plan['plans'][0];p['payload']['files']=old['payload']['files'];p['digest']=fingerprint(p['payload']);p['changed']=p['digest']!=old['digest']
    return plan

def attachment(path,sha,target,progress=lambda _:None,cancel=None):
    with closing(connect(path)) as db:row=db.execute('SELECT content FROM purchase_files WHERE sha=?',(sha,)).fetchone()
    if not row or hashlib.sha256(row[0]).hexdigest()!=sha:raise ValueError('采购原件缺失或校验失败')
    checkpoint(cancel);target=Path(target);fd,tmp=tempfile.mkstemp(dir=target.parent)
    try:
        with os.fdopen(fd,'wb') as f:f.write(row[0])
        checkpoint(cancel);os.link(tmp,target)
    finally:Path(tmp).unlink(missing_ok=True)

def mappings(path,name=None,value=None,progress=lambda _:None,cancel=None):
    checkpoint(cancel)
    if value is not None:
        import storage_chunks
        storage_chunks.backup_before_write(Path(path),progress);checkpoint(cancel)
    with closing(connect(path)) as db,db:
        if value is not None:schema(db);db.execute('INSERT INTO purchase_mappings VALUES(?,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload',(name,wr.dumps(value)))
        if not exists(db):return {}
        return {n:wr.loads(p) for n,p in db.execute('SELECT name,payload FROM purchase_mappings ORDER BY name')}

def observation(db,head):
    """Bind a follow-up acknowledgement to exactly the observed external facts."""
    links=db.execute('SELECT docid,lineid,revision,warehouse,sku,quantity FROM purchase_link_index WHERE oid=? ORDER BY docid,lineid,revision,warehouse,sku',(head['oid'],)).fetchall() if exists(db) else []
    replies=db.execute('SELECT revision,MAX(version) FROM merchant_feedback WHERE oid=? GROUP BY revision ORDER BY revision',(head['oid'],)).fetchall() if db.execute("SELECT 1 FROM sqlite_master WHERE name='merchant_feedback'").fetchone() else []
    return hashlib.sha256(wr.dumps([head['digest'],links,replies]).encode()).hexdigest()

def comparison(path,head,progress=lambda _:None,cancel=None,*,db=None,docs=None):
    if db is None:
        with closing(connect(path)) as connection:
            connection.execute('BEGIN')
            items=[current(connection,d[0]) for d in connection.execute('SELECT docid FROM purchase_documents ORDER BY rowid DESC')] if exists(connection) else []
            return comparison(path,head,progress,cancel,db=connection,docs=items)
    order=repository(path).get(head['day'],head['store'],head['revision'],view='export')
    if order['digest']!=head['digest']:raise wr.Conflict('签发快照变化')
    reply=merchant_feedback.current(db,head['oid'],head['revision'])
    values=reply['payload']['values'] if reply else {};actual={};refs={};extras={}
    for doc in docs:
        checkpoint(cancel)
        for r in doc['payload']['rows']:
            for l in r['links']:
                h=l['head']
                if (h['oid'],h['revision'],h['warehouse'])!=(head['oid'],head['revision'],head['warehouse']):continue
                actual[r['sku']]=actual.get(r['sku'],Decimal(0))+Decimal(l['quantity']);extras[r['sku']]=r['name']
                refs.setdefault(r['sku'],[]).append(r['number']+' / '+r['line_id']+f" / 采购V{doc['version']}")
    eligible={d['E']:d for ds in normal_exports.groups(order['payload']['result'],order['payload'].get('new_focus')).values() for d in ds if ao.warehouse(d)==head['warehouse']}
    rows=[]
    for sku in list(eligible)+[s for s in actual if s not in eligible]:
        d=eligible.get(sku);issued=normal_exports.sem.order_quantity(d) if d else None;returned=values.get(sku);purchased=actual.get(sku)
        rows.append([sku,d.get('D') if d else extras[sku],issued,returned,float(purchased) if purchased is not None else None,'未接入',float(purchased-Decimal(str(returned))) if purchased is not None and returned is not None else None,'额外采购' if d is None else '未关联' if purchased is None else '已关联量（未出现行不视为0）','；'.join(refs.get(sku,[]))])
    return {'head':head,'rows':rows,'feedback_version':reply['version'] if reply else None}

COMPARE_HEADERS=['商品编码','商品名称','签发建议数','加盟商确认数','已关联实际下单数','实际收货','下单−确认（已关联范围）','资料状态','采购来源']
def export_comparisons(path,heads,folder,mode,progress=lambda _:None,cancel=None):
    from xlsx_exports import stream_book,write_table
    from runtime_support import Cancelled
    if mode not in ('separate','combined'):raise ValueError('未知导出方式')
    files=[]
    try:
        checkpoint(cancel)
        with closing(connect(path)) as db:
            db.execute('BEGIN');docs=[current(db,d[0]) for d in db.execute('SELECT docid FROM purchase_documents ORDER BY rowid DESC')] if exists(db) else []
            for h in heads:ao.require_active(db,h['oid'],h['warehouse'])
            views=[comparison(path,h,progress,cancel,db=db,docs=docs) for h in heads]
    except Cancelled:return {'files':files,'cancelled':True,'error':None}
    groups=[[v] for v in views] if mode=='separate' else [views]
    for group in groups:
        if cancel and cancel.is_set():return {'files':files,'cancelled':True,'error':None}
        target=Path(folder)/('执行对照-'+(group[0]['head']['number'] if mode=='separate' else '多订单')+'-'+uuid.uuid4().hex[:8]+'.xlsx')
        rows=[]
        for v in group:
            h=v['head'];rows.extend([[h['day'],h['store'],h['warehouse'],h['number'],h['revision'],v['feedback_version'],*r] for r in v['rows']])
        fd,tmp=tempfile.mkstemp(dir=target.parent,suffix='.xlsx');os.close(fd)
        try:
            with stream_book() as book:
                write_table(book,'执行对照',['业务日期','门店编码','仓','补货单号','签发修订','回传版本']+COMPARE_HEADERS,rows,cancel=cancel,progress=progress,row_count=len(rows));book.save(tmp)
            checkpoint(cancel);os.link(tmp,target);files.append(str(target))
        except Cancelled:return {'files':files,'cancelled':True,'error':None}
        except Exception as exc:return {'files':files,'cancelled':False,'error':str(exc)}
        finally:Path(tmp).unlink(missing_ok=True)
    return {'files':files,'cancelled':False,'error':None}
