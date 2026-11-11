"""Store business eligibility. Immutable evidence, no prediction or allocation formula."""
import copy,hashlib,json,re,sqlite3
from contextlib import closing
from datetime import datetime,date,timezone,timedelta
from pathlib import Path

VERSION='admission-2'
STATES=('待核验','无资质','已核验')
def today():return datetime.now(timezone(timedelta(hours=8))).date().isoformat()
def digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False,separators=(',',':')).encode()).hexdigest()
def tags(raw):
    return list(dict.fromkeys(s.strip() for s in re.split(r'[,，;；\r\n]+',str(raw or '')) if s.strip()))
def medical(raw):return any('二类医疗' in t for t in tags(raw))

def default_exclusions(meta):
    reasons=[]
    if meta.get('product_tags_known') and '日配' in tags(meta.get('product_tags')):reasons.append('商品标签日配（默认排除）')
    if str(meta.get('export_status') or '').strip()=='禁采':reasons.append('补货建议禁采')
    return reasons
def date_value(value):
    if value in (None,''):return None
    if isinstance(value,datetime):value=value.date().isoformat()
    elif isinstance(value,date):value=value.isoformat()
    value=str(value).strip();date.fromisoformat(value);return value
def validate(record):
    if not isinstance(record,dict) or set(record)-{'qualification','refusals','name'}:raise ValueError('门店准入记录格式错误')
    q=record.get('qualification',{})
    if q.get('state','待核验') not in STATES:raise ValueError('资质状态须为待核验、无资质或已核验')
    date_value(q.get('expires'))
    if q.get('state')=='已核验' and (not str(q.get('reviewer','')).strip() or not date_value(q.get('verified_on'))):raise ValueError('已核验资质请填写核验人和核验日期')
    from purchase_io import code
    for sku,r in record.get('refusals',{}).items():
        code(sku)
        if type(r.get('active',True)) is not bool:raise ValueError('拒收状态无效')
        date_value(r.get('expires'))
        if not str(r.get('reason','')).strip():raise ValueError('请填写门店拒收原因')
    return record
def record(db,store):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='store_admission'").fetchone():return {}
    row=db.execute('SELECT payload FROM store_admission WHERE store=?',(store,)).fetchone()
    return json.loads(row[0]) if row else {}
def snapshot(path,store,*,db=None,day=None):
    if db is None:
        if not Path(path).exists():value={}
        else:
            with closing(sqlite3.connect(path)) as connection:value=record(connection,store)
    else:value=record(db,store)
    validate(value)
    return {'version':VERSION,'store':store,'day':day or today(),'revision':digest(value),'record':value}
def save(path,values,expected):
    from work_repository import Conflict,now,dumps
    from storage_chunks import backup_before_write
    from purchase_io import code
    for store,value in values.items():code(store);validate(value)
    backup_before_write(path,lambda _:None)
    with closing(sqlite3.connect(path,timeout=10)) as db,db:
        db.execute('BEGIN IMMEDIATE')
        for store in values:
            if record(db,store)!=expected.get(store,{}):raise Conflict('门店准入已被其他窗口更新，请重新核对：'+store)
        db.execute('CREATE TABLE IF NOT EXISTS store_admission(store TEXT PRIMARY KEY,payload TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS admission_events(id INTEGER PRIMARY KEY,at TEXT,store TEXT,before_value TEXT,after_value TEXT)')
        for store,value in values.items():
            db.execute('INSERT INTO admission_events(at,store,before_value,after_value) VALUES(?,?,?,?)',(now(),store,dumps(expected.get(store,{})),dumps(value)))
            db.execute('INSERT INTO store_admission VALUES(?,?) ON CONFLICT(store) DO UPDATE SET payload=excluded.payload',(store,dumps(value)))
def check(context,sku,meta):
    """Reason priority: qualification/evidence, refusal; existing disable follows."""
    q=context['record'].get('qualification',{});day=context['day'];reasons=[]
    medical_sources=meta.get('medical_sources',[]);raw=meta.get('product_tags')
    requires=bool(medical_sources) or medical(raw)
    state=q.get('state','待核验');expired=bool(q.get('expires') and q['expires']<day)
    if requires and (state!='已核验' or expired):reasons.append('二类医疗资质'+('已过期' if expired else state))
    if not meta.get('product_tags_known',False):reasons.append('商品标签资料不完整，待补资料')
    refusal=context['record'].get('refusals',{}).get(sku)
    if refusal and refusal.get('active',True) and (not refusal.get('expires') or refusal['expires']>=day):reasons.append('门店拒收：'+refusal['reason'])
    reasons.extend(default_exclusions(meta))
    return {'version':VERSION,'store':context['store'],'checked_day':day,'settings_revision':context['revision'],
            'requires_medical':requires,'tags_raw':raw,'tags':tags(raw),'tag_field_known':bool(meta.get('product_tags_known',False)),
            'medical_sources':copy.deepcopy(medical_sources),'qualification':copy.deepcopy(q),'refusal':copy.deepcopy(refusal),'reasons':reasons}
def apply(loaded,store,context):
    if context['store']!=store:raise ValueError('准入快照门店不一致')
    out={**loaded,'normalized':copy.deepcopy(loaded['normalized'])}
    for d,m in out['normalized']:
        if d['B']==store and d['_source']=='资料匹配':
            m['admission']=check(context,d['E'],m)
            d['AT']='门店拒收' if any(x.startswith('门店拒收') for x in m['admission']['reasons']) else '无有效拒收记录'
    return out
def verify_result(db,result,*,required=False):
    context=result.get('effective_settings',{}).get('admission')
    if context is None:
        if required:raise ValueError('本次结果没有门店准入依据，请重新计算后签发：'+result['store'])
        return  # Legacy/internal contract. New UI signatures explicitly require evidence.
    latest=snapshot('',result['store'],db=db)
    if latest!=context:raise ValueError('门店资质、拒收记录或核验日期已变化，请重新计算并核对：'+result['store'])
    from result_semantics import order_quantity,numeric
    for row in result['rows']:
        if row.get('_source')!='资料匹配':continue
        expected=check(context,row['E'],row['_meta'])
        if expected!=row['_meta'].get('admission'):raise ValueError('商品准入依据不一致，请重新计算：'+row['E'])
        qty=order_quantity(row)
        if expected['reasons'] and numeric(qty) and qty>0:raise ValueError('受限商品不能签发：'+row['E'])
def catalog_meta(catalogs,warehouse,sku):
    item=catalogs.get(warehouse,{}).get('items',{}).get(sku,{})
    sources=[{'warehouse':wh,'revision':c['revision'],'raw':c['items'][sku].get('商品标签'),'row':c['items'][sku].get('_row')} for wh,c in catalogs.items() if sku in c['items'] and medical(c['items'][sku].get('商品标签'))]
    return {'product_tags':item.get('商品标签'),'product_tags_known':'商品标签' in item,'medical_sources':sources}
def execution_evidence(path,store,warehouse,skus,*,catalogs=None,db=None):
    from warehouse_catalog import Repository
    catalogs=catalogs if catalogs is not None else Repository(Path(path).parent).catalogs()
    context=snapshot(path,store,db=db)
    return {'context':context,'catalog_revisions':{w:c['revision'] for w,c in catalogs.items()},
            'items':{sku:check(context,sku,catalog_meta(catalogs,warehouse,sku)) for sku in skus}}
