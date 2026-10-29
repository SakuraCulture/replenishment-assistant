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

SCHEMA=1

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
    return {**{k:head[k] for k in ('day','store','number','revision','saved')},'name':result['store_name'],'summary':result['summary']}


def project(order):
    rows=order['payload']['result']['rows']
    return {'head':{k:v for k,v in order.items() if k!='payload'},
        'summary':order['payload']['result']['summary'],
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
        try:return {'record':wr.loads(row[0]),'search':json.loads(row[1]),'projection':json.loads(row[2]) if detail else None}
        except (ValueError,TypeError):return None
    def put(self,k,record,view):
        values=[wr.dumps(record),json.dumps(view['search'],ensure_ascii=False),json.dumps(view,ensure_ascii=False)]
        with closing(self.connect()) as db,db:db.execute('INSERT OR REPLACE INTO views VALUES(?,?,?,?,?)',(k,*values,hashlib.sha256(''.join(values).encode()).hexdigest()))


def cached(path,cache,head,detail=False,cancel=None):
    idx=Index(cache);k=key(path,head);hit=idx.get(k,detail)
    if hit is not None:return hit
    before=identity(path);order=repository(path).get(head['day'],head['store'],head['revision'])
    if order is None or order['digest']!=head['digest'] or before!=identity(path):raise ValueError('归档来源已变化，请刷新')
    view=project(order);record=summary_record(head,order['payload']['result'])
    if cancel is not None and cancel.is_set():raise InterruptedError('已取消')
    idx.put(k,record,view)
    return {'record':record,'search':view['search'],'projection':view if detail else None}


def execute(path,cache,request,cancel,progress):
    repo=repository(path);kind=request['kind']
    if kind=='list':
        heads=repo.headers();records=[];matches=[];needle=request.get('search','').strip().lower()
        for i,h in enumerate(heads):
            if cancel.is_set():raise InterruptedError('已取消')
            item=cached(path,cache,h,cancel=cancel);r=item['record'];records.append(r)
            matches.append(needle in json.dumps(r,ensure_ascii=False).lower() or any(needle in s for s in item['search']))
            progress({'done':i+1,'total':len(heads)})
        if heads!=repo.headers():raise ValueError('归档列表已更新，请刷新')
        return {'records':records,'matches':matches,'heads':heads}
    head=request['head']
    if kind=='detail':
        view=cached(path,cache,head,True,cancel)['projection']
        view['head']['current_revision']=head.get('current_revision',head['revision'])
        return view
    # Audit and formal export always retrieve and validate the source, not cache.
    order=repo.get(head['day'],head['store'],head['revision'])
    if order is None or order['digest']!=head['digest']:raise ValueError('归档来源已变化，请刷新')
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
