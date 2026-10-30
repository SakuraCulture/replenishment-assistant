"""Read-only history projections; disposable cache never participates in signing/export.

All heavy functions are module-level spawn targets and have no Qt dependency.
"""
import hashlib,json,sqlite3,os,gc
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
import normal_orders as orders
import work_repository as wr
from result_semantics import fmt,order_quantity,order_amount,explanation
import new_products

SCHEMA=3
import archive_orders as ao

def repository(path):
    path=Path(path).resolve()
    repo=orders.Repository.__new__(orders.Repository)
    repo.work=SimpleNamespace(path=path,connect=lambda:sqlite3.connect(path.as_uri()+'?mode=ro',uri=True))
    return repo


def identity(path):
    p=Path(path).resolve();s=p.stat()
    return hashlib.sha256(wr.dumps([str(p),s.st_dev,s.st_ino,s.st_ctime_ns]).encode()).hexdigest()


def key(path,head):
    return wr.dumps([SCHEMA,identity(path),head['oid'],head['revision'],head['digest'],head['saved']])


def summary_record(head,result):
    from result_semantics import summarize
    children=[]
    for wh in ao.scopes(result['rows']):
        rows=[d for d in result['rows'] if ao.warehouse(d)==wh]
        children.append({'warehouse':wh,'summary':summarize(rows),'search':[(d['E']+' '+str(d.get('D',''))).lower() for d in rows]})
    return {**head,'name':result['store_name'],'summary':result['summary'],'children':children}


def project(order):
    rows=order['payload']['result']['rows']
    return {'head':{k:v for k,v in order.items() if k!='payload'},
        'summary':order['payload']['result']['summary'],
        'warehouses':[ao.warehouse(d) for d in rows],
        'rows':[[fmt(v) for v in (d['E'],d.get('D'),new_products.category(d),d.get('AS'),d.get('_candidate_qty'),order_quantity(d),order_amount(d),explanation(d))] for d in rows],
        'search':[(d['E']+' '+str(d.get('D',''))).lower() for d in rows]}


class Index:
    """Atomic per-revision publication; a broken cache is discarded, never the source."""
    def __init__(self,path):self.path=Path(path)
    def connect(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        try:
            db=sqlite3.connect(self.path);db.execute('CREATE TABLE IF NOT EXISTS views(k TEXT PRIMARY KEY,summary TEXT NOT NULL,search TEXT NOT NULL,projection TEXT NOT NULL,checksum TEXT NOT NULL)');return db
        except sqlite3.DatabaseError:
            try:db.close()
            except Exception:pass
            # Retain damaged derived data for diagnosis; no source DB operation.
            import uuid
            self.path.rename(self.path.with_name(self.path.name+'.bad-'+uuid.uuid4().hex))
            return self.connect()
    def get(self,k,detail=False):
        with closing(self.connect()) as db:
            row=db.execute('SELECT summary,search,projection,checksum FROM views WHERE k=?',(k,)).fetchone()
        if row is None:return None
        if hashlib.sha256(''.join(row[:3]).encode()).hexdigest()!=row[3]:return None
        try:
            stored=wr.loads(row[0]);record=stored
            return {'record':record,'search':json.loads(row[1]),'projection':json.loads(row[2]) if detail else None}
        except (ValueError,TypeError,KeyError):return None
    def put(self,k,record,view):
        values=[wr.dumps(record),json.dumps(view['search'],ensure_ascii=False),json.dumps(view,ensure_ascii=False)]
        with closing(self.connect()) as db,db:db.execute('INSERT OR REPLACE INTO views VALUES(?,?,?,?,?)',(k,*values,hashlib.sha256(''.join(values).encode()).hexdigest()))


def cached(path,cache,head,detail=False,cancel=None):
    idx=Index(cache);k=key(path,head);hit=idx.get(k,detail)
    if hit is not None:return hit
    before=identity(path);order=repository(path).get(head['day'],head['store'],head['revision'],view='export')
    if order is None or order['digest']!=head['digest'] or before!=identity(path):raise ValueError('归档来源已变化，请刷新')
    view=project(order);record=summary_record(head,order['payload']['result'])
    import history_forms,storage_chunks
    repo=repository(path)
    with closing(repo.work.connect()) as db:
        raw=db.execute('SELECT payload FROM normal_revisions WHERE oid=? AND revision=?',(head['oid'],head['revision'])).fetchone()
        work_revision=storage_chunks.load(raw[0],db,paths=[('work_revision',)]).get(('work_revision',))
    catalogs=history_forms.work_parts(repo,{'payload':{'work_revision':work_revision}},[('catalogs',)]).get(('catalogs',),{})
    for child in record['children']:child['source_sha256']=catalogs.get(child['warehouse'],{}).get('source_sha256')
    if cancel is not None and cancel.is_set():raise InterruptedError('已取消')
    idx.put(k,record,view)
    return {'record':record,'search':view['search'],'projection':view if detail else None}


def execute(path,cache,request,cancel,progress):
    repo=repository(path);kind=request['kind']
    if kind=='followup':
        import followup
        return followup.read(path,cache,request,cancel,progress)
    if kind=='list':
        from datetime import date
        start,end=request.get('from',''),request.get('to','')
        for value in (start,end):
            if value and date.fromisoformat(value).isoformat()!=value:raise ValueError('日期请填写YYYY-MM-DD')
        if start and end and start>end:raise ValueError('起始日期不能晚于结束日期')
        heads=repo.headers();records=[];matches=[];needle=request.get('search','').strip().lower()
        with closing(repo.work.connect()) as db:
            visibility=ao.states(db)
            retained={r[0] for r in db.execute('SELECT sha FROM source_files')} if db.execute("SELECT 1 FROM sqlite_master WHERE name='source_files'").fetchone() else set()
        for i,h in enumerate(heads):
            if cancel.is_set():raise InterruptedError('已取消')
            item=cached(path,cache,h,cancel=cancel)
            for child in item['record']['children']:
                wh=child['warehouse'];state=visibility.get((h['oid'],wh),{'deleted':False,'token':0,'at':None})
                r={k:v for k,v in item['record'].items() if k!='children'}
                r.update(warehouse=wh,parent_number=h['number'],number=ao.number(h['number'],wh),summary=child['summary'],deleted=state['deleted'],visibility_token=state['token'],visibility_at=state['at'])
                r['source_status']='已留存（导出前校验）' if child.get('source_sha256') in retained else '未留存'
                if state['deleted']!=bool(request.get('trash')):continue
                records.append(r);text=json.dumps(r,ensure_ascii=False).lower();terms=needle.split()
                match=all(t in text for t in terms) or any(all(t in text or t in sku for t in terms) for sku in child['search'])
                matches.append(match and (not start or r['day']>=start) and (not end or r['day']<=end) and (not request.get('day') or r['day']==request['day']) and (not request.get('store') or r['store']==request['store']) and (not request.get('warehouse') or wh==request['warehouse']))
            progress({'done':i+1,'total':len(heads)})
        with closing(repo.work.connect()) as db:after=ao.states(db)
        if heads!=repo.headers() or visibility!=after:raise ValueError('归档列表已更新，请刷新')
        return {'records':records,'matches':matches,'heads':heads}
    head=request['head']
    if kind=='detail':
        view=cached(path,cache,head,True,cancel)['projection']
        wh=head.get('warehouse')
        if wh:
            indices=[i for i,w in enumerate(view['warehouses']) if w==wh]
            view['rows']=[view['rows'][i] for i in indices];view['search']=[view['search'][i] for i in indices]
            meta=cached(path,cache,head,cancel=cancel)['record']
            view['summary']=next((c['summary'] for c in meta['children'] if c['warehouse']==wh),{'amount':0,'positive':0})
            view['head'].update(warehouse=wh,parent_number=view['head']['number'],number=ao.number(view['head']['number'],wh))
        with closing(repo.work.connect()) as db:state=ao.states(db).get((head['oid'],wh),{'deleted':False,'token':0})
        view['head'].update(deleted=state['deleted'],visibility_token=state['token'],current_revision=head.get('current_revision',head['revision']))
        return view
    # Audit and formal export always retrieve and validate the source, not cache.
    order=repo.get(head['day'],head['store'],head['revision'])
    if order is None or order['digest']!=head['digest']:raise ValueError('归档来源已变化，请刷新')
    if head.get('warehouse'):order={**order,'warehouse':head['warehouse']}
    if kind in ('form','form_export','attachments','source_export'):
        import history_forms,business_excel,source_archive
        if kind in ('form_export','source_export'):
            with closing(repo.work.connect()) as db:ao.require_active(db,head['oid'],head.get('warehouse'))
        if kind=='form_export' and request.get('scope')=='当时商品资料快照':raise ValueError('资料说明不是原表，请使用“下载商品资料原表”')
        if kind=='attachments':return history_forms.attachments(repo,order)
        if kind=='source_export':
            if not any(r[2]==request['sha'] for r in history_forms.attachments(repo,order)):raise ValueError('原文件不属于此签发快照')
            with closing(repo.work.connect()) as db:return source_archive.export(db,request['sha'],request['path'],cancel)
        if head.get('warehouse'):order={**order,'warehouse':head['warehouse']}
        rows=history_forms.form(repo,order,request['scope'],request.get('sku',''),cancel)
        if kind=='form_export':return business_excel.write(request['path'],[(request['scope'],['核对项目','当时值','类型'],rows)],cancel)
        return rows
    if kind=='audit':
        field=request.get('field','概要')
        if field=='概要':return {k:v for k,v in order.items() if k!='payload'}|{'payload_fields':list(order['payload']),'result_fields':list(order['payload']['result']),'rows':len(order['payload']['result']['rows'])}
        if field=='商品':
            return next((d for d in order['payload']['result']['rows'] if d['E']==request.get('sku')),None)
        if field=='参数与来源':return {k:v for k,v in order['payload']['result'].items() if k not in ('rows','loaded','intake')}
        raise ValueError('未知审计范围')
    if kind=='audit_export':
        target=Path(request['path'])
        import uuid
        temporary=target.with_name(target.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            with temporary.open('x',encoding='utf-8') as f:f.write(wr.dumps(order))
            if cancel.is_set():raise InterruptedError('已取消')
            os.link(temporary,target)  # atomic publication, refuses overwrite
        finally:temporary.unlink(missing_ok=True)
        return str(target)
    raise ValueError('未知历史读取任务')


def worker_main(conn,cancel,path,cache):
    try:
        while True:
            task=conn.recv()
            if task is None:break
            token,request=task
            try:
                result=execute(path,cache,request,cancel,lambda p:conn.send((token,'progress',p)))
                conn.send((token,'done',result))
            except Exception as exc:conn.send((token,'error',str(exc)))
            gc.collect()
    except (EOFError,BrokenPipeError):pass
    finally:conn.close()


def read_for_export(repo,head,progress=None,cancel=None):
    import engine
    engine.checkpoint(cancel)
    if progress:progress('读取并校验历史签发快照…')
    order=repo.get(head['day'],head['store'],head['revision'],view='export',progress=progress or (lambda _:None))
    engine.checkpoint(cancel)
    return order
