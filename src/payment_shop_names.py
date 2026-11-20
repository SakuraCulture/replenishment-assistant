"""Optional JST shop names, isolated from calculation parameters and order links."""
import copy,hashlib,sqlite3,unicodedata,re,uuid,time
from contextlib import closing
from pathlib import Path
import work_repository as wr
from runtime_support import checkpoint

PREFIX='payment_shop_names.'
ITEM=PREFIX+'store.'
HISTORY=PREFIX+'history.'
WAREHOUSES=('山东仓','河北仓')
BUSY_SECONDS=10

def normalized(value):return re.sub(r'\s+','',unicodedata.normalize('NFKC',str(value or '')))
def digest(value):return hashlib.sha256(wr.dumps(value).encode()).hexdigest()
def key(store,warehouse):return ITEM+digest([store,warehouse])
def connect(path):return sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=3)
def has(db,table):return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone())

def records(db):
    if not has(db,'lifecycle_mappings'):return {}
    values={}
    for name,raw in db.execute("SELECT name,payload FROM lifecycle_mappings WHERE stage='payment' AND substr(name,1,?)=? ORDER BY name",(len(ITEM),ITEM)):
        value=wr.loads(raw)
        if not isinstance(value,dict) or value.get('warehouse') not in WAREHOUSES or not isinstance(value.get('store'),str) or name!=key(value['store'],value['warehouse']) or not isinstance(value.get('name'),str) or not isinstance(value.get('version'),str):
            raise ValueError('聚水潭店铺名称资料身份无效，请在独立维护入口核对')
        values[name]=value
    return values

def load(path,known=None,progress=lambda _:None,cancel=None):
    names=dict(known or {});warnings=[]
    with closing(connect(path)) as db:
        db.execute('BEGIN');values=records(db)
        if has(db,'stores'):
            for code,raw in db.execute('SELECT code,data FROM stores ORDER BY code'):
                checkpoint(cancel);names.setdefault(code,wr.loads(raw).get('name',''))
        if has(db,'normal_orders'):
            for (code,) in db.execute('SELECT DISTINCT store FROM normal_orders ORDER BY store'):names.setdefault(code,'')
    from aoxiang_directory import load_for_settings
    try:
        for item in (load_for_settings(Path(path).parent) or {}).get('items',[]):
            if not names.get(item['code']):names[item['code']]=item['name']
    except (OSError,ValueError,KeyError,TypeError) as exc:warnings.append('本机门店目录待核：'+str(exc))
    for value in values.values():
        if not names.get(value['store']):names[value['store']]=value.get('display_name','')
    # A machine may only have migrated signed orders, with no store defaults or
    # directory. Read one latest signed snapshot per still-unnamed store.
    missing={code for code,name in names.items() if not name}
    if missing:
        from normal_orders import read_repository as repository
        import warehouse_execution as execution
        repo=repository(path)
        with closing(connect(path)) as db:
            db.execute('BEGIN')
            if has(db,'normal_orders'):
                heads=db.execute('SELECT o.day,o.store,o.oid,o.number,o.current,r.digest,r.saved FROM normal_orders o JOIN normal_revisions r ON r.oid=o.oid AND r.revision=o.current ORDER BY o.day DESC,o.oid')
                for row in heads:
                    h=dict(zip(('day','store','oid','number','revision','digest','saved'),row));code=h['store']
                    if code not in missing:continue
                    checkpoint(cancel);effective=execution.heads(db,[h]) if execution.has_parent(db,h['oid']) else [h]
                    if effective:
                        order=execution.get_order(repo,effective[0],view='export',db=db)
                        names[code]=order['payload']['result'].get('store_name','')
                    missing.remove(code)
                    if not missing:break
    checkpoint(cancel)
    return {'names':names,'records':values,'warnings':warnings}

def prepare(data,patches,clear=False,origin='manual',progress=lambda _:None,cancel=None):
    changes=[];seen=set()
    for patch in patches:
        checkpoint(cancel)
        if set(patch)!={'store','warehouse','value'}:raise ValueError('名称接口只接受门店编码、仓和店铺名称')
        code,wh,value=patch['store'],patch['warehouse'],patch['value']
        if code not in data['names']:raise ValueError('未知门店编码：'+str(code))
        if wh not in WAREHOUSES:raise ValueError('供货仓无效')
        if not isinstance(value,str) or len(value)>200 or any(ord(c)<32 for c in value):raise ValueError('店铺名称须为200字以内的单行文本')
        value=value.strip();k=key(code,wh)
        if k in seen:raise ValueError('同一门店、仓重复维护：'+code+' / '+wh)
        seen.add(k)
        if not value and not clear:continue
        before=data['records'].get(k)
        if (before or {}).get('name','')==value:continue
        changes.append({'key':k,'store':code,'warehouse':wh,'display_name':data['names'][code],
                        'before':copy.deepcopy(before),'name':value})
    plan={'changes':changes,'origin':origin};plan['digest']=digest(plan)
    return plan

def save(path,plan,progress=lambda _:None,cancel=None):
    if digest({k:v for k,v in plan.items() if k!='digest'})!=plan.get('digest'):raise ValueError('名称维护预览已变化，请重新核对')
    checkpoint(cancel)
    if not plan['changes']:return {'changed':0,'operation':None}
    from sqlite_write import begin
    from runtime_support import CommitProgress
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=rw',uri=True,timeout=.15)) as db,db:
        deadline=time.monotonic()+BUSY_SECONDS;begin(db,cancel,deadline=deadline)
        current=records(db)
        for c in plan['changes']:
            checkpoint(cancel)
            if c['key']!=key(c['store'],c['warehouse']) or current.get(c['key'])!=c['before']:
                raise wr.Conflict('该门店仓名称已变化，请刷新后重新核对：'+c['store']+' / '+c['warehouse'])
        db.execute('CREATE TABLE IF NOT EXISTS lifecycle_mappings(stage TEXT,name TEXT,payload TEXT,PRIMARY KEY(stage,name))')
        operation=uuid.uuid4().hex;now=wr.now();changes=[]
        for c in plan['changes']:
            checkpoint(cancel)
            value={'store':c['store'],'warehouse':c['warehouse'],'name':c['name'],'display_name':c['display_name'],
                   'version':uuid.uuid4().hex,'updated':now,'origin':plan['origin']}
            db.execute("INSERT INTO lifecycle_mappings VALUES('payment',?,?) ON CONFLICT(stage,name) DO UPDATE SET payload=excluded.payload",(c['key'],wr.dumps(value)))
            changes.append({'store':c['store'],'warehouse':c['warehouse'],'before':(c['before'] or {}).get('name',''),'after':c['name']})
        db.execute("INSERT INTO lifecycle_mappings VALUES('payment',?,?)",(HISTORY+operation,wr.dumps({'at':now,'origin':plan['origin'],'changes':changes})))
        checkpoint(cancel);progress(CommitProgress(True))
        while True:
            try:db.commit();break
            except sqlite3.OperationalError as exc:
                code=getattr(exc,'sqlite_errorcode',0)
                # SQLite guarantees BUSY COMMIT leaves this transaction live.
                # Retry only COMMIT on this same connection, never the writes.
                if code & 255!=sqlite3.SQLITE_BUSY or not db.in_transaction:raise
                if time.monotonic()>=deadline:
                    db.rollback()
                    raise wr.Conflict('资料仍被其他读取占用，本次名称未保存；请稍后从原预览重试') from exc
                time.sleep(.025)
    progress(CommitProgress(False))
    return {'changed':len(changes),'operation':operation}

def name_evidence(values,name):
    """A source name can identify several stores/warehouses; never choose first."""
    norm=normalized(name)
    relevant=[v for v in values.values() if norm and normalized(v['name'])==norm]
    relevant.sort(key=lambda v:(v['store'],v['warehouse']))
    return {'name':norm,'pairs':[[v['store'],v['warehouse']] for v in relevant],
            'token':digest([[v['store'],v['warehouse'],v['name'],v['version']] for v in relevant])}

def validate_evidence(db,evidence):
    if name_evidence(records(db),evidence['name'])!=evidence:
        raise wr.Conflict('本付款使用的店铺名称对应已变化，请刷新后重新识别；原付款未改动')
