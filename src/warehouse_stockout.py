"""Independent warehouse/SKU/day facts. No sales denominator or business writes."""
import json,os,sqlite3,uuid
from contextlib import contextmanager
from pathlib import Path
from stockout_common import WAREHOUSES,encoded,now,today,digest,file_hash,date_of,resource_check,atomic_json,read_json
from runtime_support import checkpoint

VERSION='warehouse-stockout-2'
FIELDS=('商品编码','商品名称','可用数','商品状态','虚拟分类','商品标签','分类','一级分类','二级分类','三级分类','供应商编码','供应商名称')
DIMENSIONS={'warehouse':('warehouse','供货仓'),'supplier_code':('supplier_code','供应商编码'),'supplier':('supplier','供应商名称'),
    'category':('category','商品分类'),'category1':('category1','一级分类'),'category2':('category2','二级分类'),
    'category3':('category3','三级分类'),'sku':('sku','商品编码'),'name':('name','商品名称'),
    'day':('day','观测日期'),'status':('status','商品状态'),'virtual_tags':('virtual_tags','虚拟分类'),'product_tags':('product_tags','商品标签')}


class Repository:
    def __init__(self,root):
        self.root=Path(root);self.folder=self.root/'analysis'/'warehouse_stockout';self.path=self.folder/'warehouse_stockout.sqlite3'

    @contextmanager
    def connect(self,write=False):
        db=sqlite3.connect(self.path if write else self.path.resolve().as_uri()+'?mode=ro',uri=not write,timeout=10)
        db.row_factory=sqlite3.Row;db.execute('PRAGMA foreign_keys=ON');db.execute('PRAGMA cache_size=-8192');db.execute('PRAGMA temp_store=FILE')
        from stockout_tags import assessment
        db.create_function('analysis_assessment',1,lambda raw:assessment(raw) or 0,deterministic=True)
        try:
            if not write:db.execute('PRAGMA query_only=ON')
            yield db
            if write:db.commit()
        except BaseException:
            if write:db.rollback()
            raise
        finally:db.close()

    def initialize(self):
        self.folder.mkdir(parents=True,exist_ok=True)
        with self.connect(True) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS originals(sha TEXT PRIMARY KEY,path TEXT,bytes INTEGER);
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,day TEXT,basis TEXT,state TEXT,message TEXT,created TEXT,updated TEXT,external TEXT,catalogs TEXT,log_deleted INTEGER DEFAULT 0,source_run TEXT);
            CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY,warehouse TEXT,day TEXT,observed TEXT,source_sha TEXT,rows INTEGER,version TEXT);
            CREATE TABLE IF NOT EXISTS run_snapshots(run TEXT,warehouse TEXT,snapshot TEXT,PRIMARY KEY(run,warehouse));
            CREATE TABLE IF NOT EXISTS current(day TEXT,warehouse TEXT,snapshot TEXT,run TEXT,PRIMARY KEY(day,warehouse));
            CREATE TABLE IF NOT EXISTS facts(snapshot TEXT REFERENCES snapshots(id) ON DELETE CASCADE,sku TEXT,name TEXT,stock_raw TEXT,stock REAL,status TEXT,virtual_tags TEXT,product_tags TEXT,category TEXT,category1 TEXT,category2 TEXT,category3 TEXT,supplier_code TEXT,supplier TEXT,excluded INTEGER,bad INTEGER,reason TEXT,PRIMARY KEY(snapshot,sku));
            CREATE INDEX IF NOT EXISTS fact_dimensions ON facts(snapshot,category,supplier);
            CREATE TABLE IF NOT EXISTS cleanup(sha TEXT PRIMARY KEY,path TEXT,error TEXT);''')

    def selected(self):
        path=self.folder/'selection.json'
        return warehouses(read_json(path)) if path.exists() else list(WAREHOUSES)

    def select(self,values):atomic_json(self.folder/'selection.json',warehouses(values));return self.selected()

    def settings(self,basis):return json.loads(basis)

    def new_run(self,values,*,source=None,day=None,run_id=None):
        values=warehouses(values);rid=run_id or uuid.uuid4().hex
        if run_id:
            with self.connect() as db:prior=db.execute('SELECT basis,source_run FROM runs WHERE id=?',(rid,)).fetchone()
            if prior:
                if (json.loads(prior['basis'])['warehouses'],prior['source_run'])!=(values,source):raise ValueError('大仓任务身份冲突')
                return rid
        with self.connect(True) as db:db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)',(rid,day or today(),encoded({'warehouses':values}),'PREPARED','',now(),now(),'{}','{}',0,source))
        return rid

    def run(self,rid):
        with self.connect() as db:row=db.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone()
        if row is None:raise ValueError('分析作业不存在，请刷新')
        value=dict(row)
        for key in ('external','catalogs'):value[key]=json.loads(value[key])
        return value

    def change(self,rid,**values):
        if set(values)-{'state','message','external','catalogs'}:raise ValueError('未知作业字段')
        values={k:encoded(v) if k in ('external','catalogs') else v for k,v in values.items()};values['updated']=now()
        with self.connect(True) as db:
            if db.execute('UPDATE runs SET '+','.join(k+'=?' for k in values)+' WHERE id=?',(*values.values(),rid)).rowcount!=1:raise ValueError('原分析作业已删除')

    def retain(self,source,sha,cancel=None):
        source=Path(source);target=self.folder/'originals'/(sha+'.xlsx');target.parent.mkdir(exist_ok=True)
        if target.exists():
            if file_hash(target)!=sha:raise ValueError('分析原件校验失败')
        else:
            resource_check(target.parent,source.stat().st_size);temp=target.with_suffix('.'+uuid.uuid4().hex+'.tmp')
            try:
                with source.open('rb') as src,temp.open('xb') as dst:
                    while data:=src.read(512*1024):checkpoint(cancel);dst.write(data)
                    dst.flush();os.fsync(dst.fileno())
                if file_hash(temp)!=sha:raise ValueError('仓资料在读取期间变化，未采用')
                os.replace(temp,target)
            finally:temp.unlink(missing_ok=True)
        with self.connect(True) as db:db.execute('INSERT OR IGNORE INTO originals VALUES(?,?,?)',(sha,str(target),target.stat().st_size))
        return target

    def publish(self,rid,wh,*,once=False):
        with self.connect(True) as db:
            row=db.execute('SELECT s.* FROM snapshots s JOIN run_snapshots r ON r.snapshot=s.id WHERE r.run=? AND r.warehouse=?',(rid,wh)).fetchone()
            if row is None:raise ValueError('该仓没有完整的分析快照')
            if once:
                ext=json.loads(db.execute('SELECT external FROM runs WHERE id=?',(rid,)).fetchone()[0])
                if wh in ext.get('deleted_warehouses',[]):raise ValueError('原大仓分析已删除，旧任务不会恢复它')
                published=ext.setdefault('published_warehouses',{})
                if wh in published:
                    if published[wh]!=row['id']:raise ValueError('原仓发布身份已变化')
                    return  # Never rewind a later independent publication.
                published[wh]=row['id']
                db.execute('UPDATE runs SET external=? WHERE id=?',(encoded(ext),rid))
            db.execute('INSERT INTO current VALUES(?,?,?,?) ON CONFLICT(day,warehouse) DO UPDATE SET snapshot=excluded.snapshot,run=excluded.run',(row['day'],wh,row['id'],rid))


def warehouses(values):
    if not isinstance(values,list) or not values or len(values)!=len(set(values)) or set(values)-set(WAREHOUSES):raise ValueError('请选择山东仓、河北仓，至少一仓')
    return values


def load_master(repo,source,warehouse,*,run,expected=None,expected_rows=None,observed=None,cancel=None,progress=lambda _:None):
    from stockout_facts import table,identifier
    from stockout_common import number
    from admission import tags
    warehouses([warehouse]);sha=expected or file_hash(source);path=repo.retain(source,sha,cancel)
    observed=observed or now();day=date_of(observed);sid=uuid.uuid4().hex;count=0
    if repo.run(run)['day']!=day:raise ValueError('采集已跨日或原件观测日期不符，未将本次库存补入其他日期；请结束原准备后采集今日')
    with table(path,('商品编码','可用数'),cancel,used=FIELDS) as (headers,rows),repo.connect(True) as db:
        db.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?,?)',(sid,warehouse,day,observed,sha,0,VERSION))
        batch=[]
        for line,row in rows:
            checkpoint(cancel);sku=identifier(row['商品编码']);count+=1
            missing=[h for h in ('虚拟分类','商品标签','商品状态') if h not in headers]
            retired=any('淘汰' in t for t in tags(row.get('虚拟分类')));daily='日配' in tags(row.get('商品标签'))
            status=str(row.get('商品状态') or '').strip();disabled=status=='禁用';stock=number(row.get('可用数'))
            from stockout_tags import assessment
            exam=assessment(row.get('虚拟分类') or '')
            excluded=bool(retired or daily or disabled or exam);reasons=[]
            if retired:reasons.append('虚拟分类含淘汰')
            if daily:reasons.append('商品标签日配')
            if disabled:reasons.append('仓商品禁用')
            if exam:reasons.append('虚拟分类淘便考核')
            if missing:reasons.append('缺少字段：'+'、'.join(missing))
            if not status:reasons.append('商品状态未填写')
            if stock is None:reasons.append('可用库存缺失或无效')
            bad=not excluded and bool(missing or not status or stock is None)
            strings=[str(row.get(k) or '') for k in ('商品名称','商品状态','虚拟分类','商品标签','分类','一级分类','二级分类','三级分类','供应商编码','供应商名称')]
            name,status,virtual,product,cat,c1,c2,c3,scode,supplier=strings
            batch.append((sid,sku,name,str(row.get('可用数') if row.get('可用数') is not None else ''),stock,status,virtual,product,cat,c1,c2,c3,scode,supplier,int(excluded),int(bad),'；'.join(reasons)))
            if len(batch)>=1000:
                db.executemany('INSERT INTO facts VALUES('+','.join('?'*17)+')',batch);batch.clear();progress(f'{warehouse}已读取{count:,}款');resource_check(repo.folder)
        if batch:db.executemany('INSERT INTO facts VALUES('+','.join('?'*17)+')',batch)
        if not count:raise ValueError('仓资料没有商品行')
        if expected_rows is not None and count!=expected_rows:raise ValueError('仓原件商品行数与回执不一致')
        db.execute('UPDATE snapshots SET rows=? WHERE id=?',(count,sid));checkpoint(cancel)
        db.execute('INSERT INTO run_snapshots VALUES(?,?,?) ON CONFLICT(run,warehouse) DO UPDATE SET snapshot=excluded.snapshot',(run,warehouse,sid))
    return sha


def validate(options):
    from datetime import date
    result=dict(options);start=date.fromisoformat(result['start']);end=date.fromisoformat(result['end'])
    if not 0<=(end-start).days<=366:raise ValueError('请选择最多367天的有效日期范围')
    filters=result.get('filters',{})
    if not isinstance(filters,dict) or set(filters)-set(DIMENSIONS):raise ValueError('筛选维度无效')
    for values in filters.values():
        if not isinstance(values,list) or len(values)>1000 or any(not isinstance(v,str) or len(v)>65536 for v in values):raise ValueError('筛选值无效或超过1000项')
    if result.get('mode','all') not in ('all','included','excluded','pending'):raise ValueError('商品范围无效')
    return result


def dimensions(keys):
    if not isinstance(keys,(list,tuple)) or not 1<=len(keys)<=len(DIMENSIONS) or len(keys)!=len(set(keys)) or set(keys)-set(DIMENSIONS):raise ValueError('请选择不重复的有效维度')
    result=list(keys)
    if 'sku' in result and 'name' not in result:result.insert(result.index('sku')+1,'name')
    if 'supplier' in result and 'supplier_code' not in result:result.insert(result.index('supplier'),'supplier_code')
    return result


def where(options):
    options=validate(options);parts=['s.day BETWEEN ? AND ?'];values=[options['start'],options['end']]
    for key,choices in options.get('filters',{}).items():
        if choices:
            col=('s.' if key in ('day','warehouse') else 'f.')+DIMENSIONS[key][0]
            parts.append('coalesce('+col+",'') IN ("+','.join('?'*len(choices))+')');values.extend(choices)
    text=options.get('search','').strip()
    if text:
        if len(text)>200:raise ValueError('搜索内容过长')
        escaped=text.replace('!','!!').replace('%','!%').replace('_','!_')
        parts.append("(f.sku LIKE ? ESCAPE '!' OR f.name LIKE ? ESCAPE '!' OR f.supplier LIKE ? ESCAPE '!')");values.extend(['%'+escaped+'%']*3)
    mode=options.get('mode','all')
    if mode=='included':parts.append('f.excluded=0')
    if mode=='excluded':parts.append('f.excluded=1')
    if mode=='pending':parts.append('f.bad=1')
    return ' AND '.join(parts),values


EFFECTIVE_FACTS='''(SELECT snapshot,sku,name,stock_raw,stock,status,virtual_tags,product_tags,
 category,category1,category2,category3,supplier_code,supplier,
 CASE WHEN analysis_assessment(virtual_tags)=1 THEN 1 ELSE excluded END AS excluded,
 CASE WHEN analysis_assessment(virtual_tags)=1 THEN 0 ELSE bad END AS bad,
 CASE WHEN analysis_assessment(virtual_tags)=1 AND instr(coalesce(reason,''),'虚拟分类淘便考核')=0
 THEN CASE WHEN coalesce(reason,'')='' THEN '' ELSE reason||'；' END||'虚拟分类淘便考核'
 ELSE reason END AS reason FROM facts)'''
LINK=' FROM current c JOIN snapshots s ON c.snapshot=s.id JOIN '+EFFECTIVE_FACTS+' f ON f.snapshot=s.id '
SUMS='count(*) total,sum(f.excluded=0) base,sum(f.excluded=0 AND f.stock<=0) zero,sum(f.bad) pending,sum(f.excluded) excluded'


def metric(row):
    value={k:row[k] or 0 for k in ('total','base','zero','pending','excluded')}
    value['rate']=value['zero']/value['base'] if value['base'] and not value['pending'] else None
    return value


def query(repo,options):
    clause,values=where(options)
    with repo.connect() as db:
        totals=metric(db.execute('SELECT '+SUMS+LINK+'WHERE '+clause,values).fetchone())
        totals['coverage']=[dict(r) for r in db.execute('SELECT s.day,s.warehouse,s.observed,s.rows FROM current c JOIN snapshots s ON c.snapshot=s.id WHERE s.day BETWEEN ? AND ? ORDER BY s.day,s.warehouse',(options['start'],options['end']))]
    return totals


def iter_cuts(repo,options,keys,*,cancel=None):
    keys=dimensions(keys);clause,values=where(options)
    columns=[('s.' if k in ('day','warehouse') else 'f.')+DIMENSIONS[k][0] for k in keys]
    selection=','.join(col+' AS "'+key+'"' for col,key in zip(columns,keys))
    with repo.connect() as db:
        db.set_progress_handler(lambda:1 if cancel is not None and cancel.is_set() else 0,2000)
        for row in db.execute('SELECT '+selection+','+SUMS+LINK+'WHERE '+clause+' GROUP BY '+','.join(columns)+' ORDER BY '+','.join(columns),values):
            checkpoint(cancel);yield {**{k:row[k] or '' for k in keys},**metric(row)}


def cuts(repo,options,keys,*,offset=0,cancel=None):
    from itertools import islice
    if not isinstance(offset,int) or not 0<=offset<=10000000:raise ValueError('分页范围无效')
    rows=list(islice(iter_cuts(repo,options,keys,cancel=cancel),offset,offset+201))
    return {'rows':rows[:200],'more':len(rows)>200,'offset':offset,'dimensions':dimensions(keys)}


def iter_details(repo,options,*,cancel=None):
    clause,values=where(options)
    with repo.connect() as db:
        db.set_progress_handler(lambda:1 if cancel is not None and cancel.is_set() else 0,2000)
        for row in db.execute('SELECT s.day,s.warehouse,s.observed,f.*'+LINK+'WHERE '+clause+' ORDER BY s.day,s.warehouse,f.sku',values):checkpoint(cancel);yield dict(row)


def detail(repo,options,*,offset=0,cancel=None):
    from itertools import islice
    if not isinstance(offset,int) or not 0<=offset<=10000000:raise ValueError('分页范围无效')
    rows=list(islice(iter_details(repo,options,cancel=cancel),offset,offset+201))
    return {'rows':rows[:200],'more':len(rows)>200,'offset':offset}


def filter_values(repo,options,key,search=''):
    dimensions([key]);local={**options,'filters':{k:v for k,v in options.get('filters',{}).items() if k!=key}}
    result=[]
    for row in iter_cuts(repo,local,[key]):
        if search.casefold() in row[key].casefold():result.append(row[key])
        if len(result)>1000:break
    return {'values':result[:1000],'more':len(result)>1000}
