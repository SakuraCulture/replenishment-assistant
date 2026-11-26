"""Five-stage analytical projection. Reads frozen facts; never mutates business data.

No Qt, model execution, current catalog or current must-sell list is used here.
Quantity/price arithmetic uses Decimal; JSON/Excel conversion is presentation only.
"""
from collections import OrderedDict
from contextlib import closing
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
import hashlib
import io
import json
import re
import sqlite3

import archive_orders as ao
import lifecycle_comparison as comparison
import order_lifecycle as lifecycle
import result_semantics as sem
import source_archive
import storage_chunks as chunks
import warehouse_execution as execution
from runtime_support import checkpoint
from priority_list import CATALOG_SOURCE
from export_progress import RowProgress
import analytics_lifecycle_scope as lifecycle_scope

VERSION = 'funnel-4'
STAGES = ('model', 'signed', 'return', 'payment', 'receipt')
NAMES = dict(model='模型建议', signed='签发', **{'return':'门店确认'}, payment='实际下单', receipt='实际收货')
GROUPS = ('必采', '非必采', '属性待核')
PAIRS = tuple(zip(STAGES, STAGES[1:]))
# Exact tokens observed in the two original warehouse exports, not fuzzy substrings.
from virtual_categories import RETIRED
num = comparison.num
amount = comparison.amount


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False, default=str).encode()).hexdigest()


def query_spec(spec):
    allowed = {'from','to','stores','orders','warehouse','group','search','category','rounds','coverage_through','as_of','lifecycle_from'}
    if set(spec)-allowed: raise ValueError('存在未开放的分析筛选字段')
    out = {**spec}
    for k in ('from','to'):
        if not out.get(k): raise ValueError('请指定分析开始和结束日期')
        date.fromisoformat(out[k])
    if out['from'] > out['to']: raise ValueError('开始日期不能晚于结束日期')
    for key in ('stores','orders'):
        values = out.get(key, [])
        if not isinstance(values,list) or any(not isinstance(x,str) or not x for x in values):
            raise ValueError('门店、订单须使用精确编码列表')
        out[key] = list(dict.fromkeys(values))
    if out.get('warehouse') not in (None, '', '山东仓', '河北仓'): raise ValueError('未知供货仓')
    if out.get('group') not in (None, '', *GROUPS): raise ValueError('未知必采分组')
    lifecycle_scope.validate(out.get('lifecycle_from', ''))
    if type(out.get('rounds',8)) is not int or not 1<=out.get('rounds',8)<=30:raise ValueError('复盘轮数须在1至30之间')
    if out.get('coverage_through', 'signed') not in STAGES: raise ValueError('未知天数比较环节')
    if out.get('as_of'):
        at=datetime.fromisoformat(out['as_of'].replace('Z','+00:00'))
        if at.tzinfo is None:raise ValueError('历史截至时间须包含时区，例如2026-11-12T12:00:00+08:00')
        out['as_of']=at.astimezone(timezone(timedelta(hours=8))).isoformat(timespec='seconds')
    return out


class Evidence:
    """One frozen original parse per source within a query; no filesystem fallback."""
    def __init__(self, db, cancel=None):
        self.db, self.cancel = db, cancel
        self.work, self.sources = OrderedDict(), OrderedDict()

    def catalogs(self, revision):
        if revision not in self.work:
            r = self.db.execute('SELECT payload FROM normal_work_revisions WHERE revision=?', (revision,)).fetchone()
            self.work[revision] = chunks.load(r[0], self.db, paths=[('catalogs',)]).get(('catalogs',), {}) if r else {}
            if len(self.work)>2: self.work.popitem(last=False)
        self.work.move_to_end(revision)
        return self.work[revision]

    def catalog(self, order, warehouse):
        return self.catalogs(order['payload'].get('work_revision')).get(warehouse, {})

    def source(self, catalog):
        sha, sheet = catalog.get('source_sha256'), catalog.get('sheet')
        key = (sha, sheet)
        if key not in self.sources:
            record = source_archive.read(self.db, sha) if sha else None
            result = {};heads=[]
            if record:
                import openpyxl
                from input_adapter import identifier
                book = openpyxl.load_workbook(io.BytesIO(record[1]),read_only=True,data_only=True,keep_links=False)
                try:
                    if sheet not in book.sheetnames: raise ValueError('当时仓资料工作表不一致')
                    ws=book[sheet];ws.reset_dimensions();it=ws.iter_rows(values_only=True)
                    heads=[str(v).strip() if v is not None else '' for v in next(it,())]
                    fields=('商品编码','商品状态','虚拟分类','供应商名称','分类','可用数')
                    if heads.count('商品编码')==1:
                        indexes={k:heads.index(k) for k in fields if heads.count(k)==1}
                        for rownum, values in enumerate(it,2):
                            checkpoint(self.cancel)
                            raw={k:values[i] if i<len(values) else None for k,i in indexes.items()}
                            sku,error=identifier(raw['商品编码'])
                            if error: continue
                            result[sku] = None if sku in result else {**raw,'source_row':rownum}
                finally: book.close()
            self.sources[key]={'items':result,'headers':heads,'present':bool(record)}
            if len(self.sources)>4:self.sources.popitem(last=False)
        self.sources.move_to_end(key)
        return self.sources[key]

    def original(self,catalog):
        source=self.source(catalog)
        # Preserve the original admission evidence contract independently of
        # optional supplier/category enrichment.
        return source['items'] if all(source['headers'].count(k)==1 for k in ('商品编码','商品状态','虚拟分类')) else {}

    def dimensions(self,order,warehouse,sku,row):
        from funnel_dimensions import attributes
        catalog=self.catalog(order,warehouse)
        return attributes(row,catalog,sku,self.source(catalog))

    def template_attributes(self,order,warehouse,sku,row):
        """Project retained input facts, never present-day settings or stock."""
        from input_adapter import identifier
        row=row or {};catalog=self.catalog(order,warehouse);source=self.source(catalog)
        frozen=catalog.get('items',{}).get(sku) or {};original=source['items'].get(sku)
        heads=source['headers'];notes=[];values=[]
        if source['present']:
            if heads.count('商品编码')>1:notes.append('原件商品编码表头重复')
            if heads.count('可用数')>1:notes.append('原件可用数表头重复')
            if sku in source['items'] and original is None:notes.append('原件商品编码重复')
        for title,record in (('冻结仓资料',frozen),('归档原件',original or {})):
            if '可用数' not in record:continue
            value=num(record['可用数'])
            if value is None:notes.append(title+'可用数缺失或无效')
            else:values.append((title,value))
        if len({value for _,value in values})>1:notes.append('冻结仓资料与归档原件可用数冲突')
        stock=values[0][1] if values and not notes else None
        if not values and not notes:notes.append('当时可用数未留存')
        barcode,error=identifier(row.get('F'))
        payload=order['payload']
        model_version=sem.saved_model_version(payload['result'],payload.get('model_version'))
        # Earlier reports used the retained quantile when present. Keep that historical value.
        target=None if sem.poisson_mean_field(model_version)=='AO' else num(row.get('BF'))
        if target is None:
            predicted,safety=num(row.get('AP')),num(row.get('AL'))
            if predicted is not None and safety is not None:target=predicted+safety
        minimum=row.get('_mandatory_holding') or {}
        meta=row.get('_meta') or {}
        return {'barcode':None if error else barcode,'supply_state':template_text(row.get('S')),
            'stock':num(row.get('K')),'monthly_sales':num(row.get('J')),'in_transit':num(row.get('L')),
            'target_holding':target,'pack_size':num(row.get('BD')),
            'minimum_holding':num(minimum.get('minimum')) if isinstance(minimum,dict) else None,
            'stock_snapshot_at':template_text(meta.get('export_updated')),'warehouse_stock':stock,
            'warehouse_stock_basis':{'catalog_revision':catalog.get('revision'),
                'source_sha256':catalog.get('source_sha256'),'sheet':catalog.get('sheet'),
                'catalog_imported_at':catalog.get('imported_at'),
                'source_row':original.get('source_row') if original else None,
                'sources':[title for title,_ in values] if stock is not None else [],'notes':notes}}

    def item(self, order, warehouse, sku):
        c=self.catalog(order,warehouse)
        row=c.get('items',{}).get(sku)
        if row and '虚拟分类' in row: return row, c.get('revision')
        return self.original(c).get(sku), c.get('source_sha256')


def template_text(value):
    if isinstance(value,str):return value if value.strip() else None
    if isinstance(value,(date,datetime)):return value.isoformat()
    return None


def membership(result, row, sku, warehouse=None):
    listing=result.get('must_list')
    if not listing or not listing.get('revision'): return '属性待核'
    if row is not None and type(row.get('_must_sell')) is bool:
        return '必采' if row['_must_sell'] else '非必采'
    audit=result.get('must_list_audit')
    if listing.get('source')==CATALOG_SOURCE:
        if row is not None or not isinstance(audit,list):
            return '属性待核'
        source=listing['warehouses'].get(warehouse)
        if not source:
            return '属性待核'
        codes={item['sku'] for item in audit if item.get('warehouse')==warehouse}
        if sku in codes:
            return '必采'
        return '非必采' if not source['missing_tag_fields'] and len(codes)==source['count'] else '属性待核'
    if isinstance(audit,list) and len({r['sku'] for r in audit})==listing.get('total'):
        return '必采' if any(r['sku']==sku for r in audit) else '非必采'
    return '属性待核'


def inclusion(row, master):
    reasons=[];missing=[]
    if row is not None:
        meta=row.get('_meta',{})
        if meta.get('export_status')=='停采': reasons.append('门店停采')
        if row.get('S')=='禁用': reasons.append('对应仓禁用')
        admission=meta.get('admission',{})
        for reason in admission.get('reasons',[]):
            if '资料不完整' in reason: missing.append(reason)
            else: reasons.append(reason)
        if 'export_status' not in meta: missing.append('门店禁采依据未留存')
    else: missing.append('追加商品门店准入依据未留存')
    if master is None or '虚拟分类' not in master:
        missing.append('当时虚拟分类未留存或商品编码不唯一')
    else:
        tags=[v.strip() for v in re.split(r'[,，;；\r\n]+',str(master.get('虚拟分类') or '')) if v.strip()]
        if any(t in RETIRED for t in tags): reasons.append('对应仓淘汰品')
        if master.get('商品状态')=='禁用': reasons.append('对应仓禁用')
    if row is not None and 'S' not in row and (master is None or '商品状态' not in master):
        missing.append('对应仓状态未留存')
    return ('排除' if reasons else '待核' if missing else '纳入'),list(dict.fromkeys(reasons+missing))


def build_facts(order, head, view, evidence, cancel=None, *, spec=None, mode='full'):
    result=order['payload']['result']
    raw=[r for r in result['rows'] if ao.warehouse(r)==head['warehouse']]
    bysku={r['E']:r for r in raw}
    if len(bysku)!=len(raw): raise ValueError('签发快照商品编码重复，不能合计')
    maps={s:{r['sku']:r for r in d['payload']['rows']} if d else {} for s,d in view['documents'].items()}
    payment=view['documents']['payment']
    paid_rows=maps['payment']
    ordered=view['documents'].get(lifecycle.JST_ORDER) if view['health'].get(lifecycle.JST_ORDER) else None
    if ordered:
        maps['payment']=maps[lifecycle.JST_ORDER]
    legacy=view.get('legacy_return')
    if not view['documents']['return'] and legacy:
        maps['return']={sku:{'base_quantity':q} for sku,q in legacy['values'].items()}
    skus=list(bysku);seen=set(skus)
    for s in lifecycle.ALL_STAGES:
        for sku in maps.get(s,{}):
            if sku not in seen:
                skus.append(sku);seen.add(sku)
    states={'model':('当时自动建议',True),'signed':('已签发',True),
            **{s:comparison.stage_state(view,s) for s in lifecycle.STAGES}}
    if ordered:
        states['payment']=('聚水潭已客审',True)
    spec=spec or {}
    terms=spec.get('search','').casefold().split()
    if mode=='analysis_detail':
        from analytics_template_export import retain_detail_row
        from workflow_analysis import set_operations
    rows=[]
    for sku in skus:
        checkpoint(cancel);r=bysku.get(sku)
        name=r.get('D') if r else next((maps[s][sku].get('name','') for s in lifecycle.ALL_STAGES if sku in maps.get(s,{})),'')
        category=r.get('U') if r else None
        if spec.get('category') and category!=spec['category']:continue
        if terms and not all(t in (result['store_name']+' '+head['number']+' '+sku+' '+str(name)).casefold() for t in terms):continue
        group=membership(result,r,sku,head.get('warehouse'))
        if spec.get('group') and group!=spec['group']:continue
        master,basis=evidence.item(order,head['warehouse'],sku)
        scope,reasons=inclusion(r,master)
        if scope=='排除' and mode in ('detail','analysis_detail'):continue
        quantities={'model':num(r.get('AS')) if r else None,'signed':num(sem.order_quantity(r)) if r else Decimal(0)}
        for s in lifecycle.STAGES:
            doc=(ordered or payment) if s=='payment' else view['documents'][s]
            if sku in maps[s]: quantities[s]=num(maps[s][sku].get('base_quantity'))
            elif doc and doc['payload'].get('schema')==lifecycle.SCHEMA: quantities[s]=Decimal(0)
            else: quantities[s]=None
        price=num(r.get('V')) if r else None
        row={'day':head['day'],'store':head['store'],'store_name':result['store_name'],
            'warehouse':head['warehouse'],'order':head['number'],'sku':sku,'name':name,'unit':r.get('G') if r else None,
            'order_status':view['status'],'order_next':view['next'],
            'category':category,'group':group,'scope':scope,'reasons':reasons,
            'price':price,'quantities':quantities,'valid':{s:bool(v[1]) and quantities[s] is not None for s,v in states.items()},
            'states':{s:v[0] for s,v in states.items()},'master_basis':basis,
            'observations':{k:r.get(k) for k in ('H','I','J','K','L','N','AN','AM','AG','AH','BD','AL')} if r else {},
            'decision':{'budget':r.get('_budget_reason'),'manual':r.get('_preissue',{}).get('reason'),
                        'new':r.get('_new_requested'),'one_box':r.get('_audit',{}).get('applied')} if r else {},
            'payment_amount':num(paid_rows.get(sku,{}).get('amount')),
            'payment_currency':paid_rows.get(sku,{}).get('currency') or '未标币种'}
        if ordered:
            row['payment_valid']=bool(view['health']['payment'] and (
                paid_rows.get(sku,{}).get('base_quantity') is not None or payment['payload'].get('schema')==lifecycle.SCHEMA))
        if mode=='business_daily':
            row['order_values']={}
            for stage in ('return','payment'):
                if not row['valid'][stage]:
                    continue
                value=num(maps[stage].get(sku,{}).get('amount'))
                row['order_values'][stage]=Decimal(0) if value is None and quantities[stage]==0 else value
        if mode=='analysis_detail':
            set_operations(row,view,maps.get('operations',{}).get(sku,{}))
            if not retain_detail_row(row):continue
        if mode!='overview':
            row.update(source_row=r.get('_meta',{}).get('export_row') if r else None,
                       payment_source=view['documents']['payment']['payload'].get('source_kind','原付款单据') if view['documents']['payment'] else None,
                       notes={s:maps[s].get(sku,{}).get('processing_note','') for s in lifecycle.STAGES},
                       **evidence.dimensions(order,head['warehouse'],sku,r))
        if mode in ('full','analysis_detail'):
            row['template_attributes']=evidence.template_attributes(order,head['warehouse'],sku,r)
        rows.append(row)
    return rows


def aggregate(rows):
    """Ratios always use the same priced valid row cohort in both stages."""
    eligible=[r for r in rows if r['scope']=='纳入']
    complete_by_order={};complete_rows=0
    for row in eligible:
        order=row['order'];complete=all(row['valid'][s] for s in STAGES)
        complete_by_order[order]=complete_by_order.get(order,True) and complete
        complete_rows+=complete
    out={'rows':len(rows),'included':len(eligible),'included_orders':len(complete_by_order),
         'complete_orders':sum(complete_by_order.values()),
         'complete_rows':complete_rows,'excluded':sum(r['scope']=='排除' for r in rows),
         'pending':sum(r['scope']=='待核' for r in rows),'stages':{},'pairs':[]}
    for s in STAGES:
        valid=[r for r in eligible if r['valid'][s]]
        values=[amount(r['quantities'][s],r['price']) for r in valid]
        priced=[v for v in values if v is not None]
        out['stages'][s]={'value':sum(priced,Decimal(0)) if priced else None,
            'valid_rows':len(valid),'unpriced':sum(v is None for v in values),
            'skus':len({r['sku'] for r in valid if r['quantities'][s]>0}) if valid else None,
            'orders':len({r['order'] for r in valid})}
    for a,b in PAIRS:
        before=after=up=down=Decimal(0);count=0;changed=0;orders=set()
        for r in eligible:
            if not r['valid'][a] or not r['valid'][b]:continue
            x,y=(amount(r['quantities'][s],r['price']) for s in (a,b))
            if x is None or y is None:continue
            count+=1;orders.add(r['order']);before+=x;after+=y
            delta=y-x;up+=max(delta,0);down+=max(-delta,0);changed+=r['quantities'][a]!=r['quantities'][b]
        out['pairs'].append({'from':a,'to':b,'rows':count,'orders':len(orders),'changed':changed,
            'before':before if count else None,'after':after if count else None,'increase':up if count else None,
            'decrease':down if count else None,'delta':after-before if count else None,
            'ratio':after/before if count and before>0 else None})
    pay={}
    for r in eligible:
        if r.get('payment_valid',r['valid']['payment']):
            g=pay.setdefault(r['payment_currency'],{'amount':Decimal(0),'missing':0})
            if r['payment_amount'] is None:g['missing']+=1
            else:g['amount']+=r['payment_amount']
    out['payment']=pay
    return out


def summaries(rows,level):
    from analytics_identifiers import summary
    groups=OrderedDict()
    for r in rows:
        key=(r['store'],r['warehouse'],r['order']) if level=='order' else (r['store'],r['warehouse']) if level=='warehouse' else (r['store'],)
        groups.setdefault(key,[]).append(r)
    return [{'key':key,'store':rs[0]['store'],'name':rs[0]['store_name'],
             'warehouse':rs[0]['warehouse'] if level!='store' else ' / '.join(dict.fromkeys(r['warehouse'] for r in rs)),
             'order':rs[0]['order'] if level=='order' else '',
             'order_links':summary(rs),
             'days':sorted({r['day'] for r in rs}),'metrics':aggregate(rs),
             'groups':{g:aggregate([r for r in rs if r['group']==g]) for g in GROUPS}} for key,rs in groups.items()]


def data_stamp(db,spec):
    """Lightweight business-index check, excluding unrelated AI task journal writes."""
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'normal_orders' not in tables:return digest([])
    selection="SELECT oid FROM normal_orders WHERE day>=? AND day<=? AND kind NOT IN ('material','policy','opening')"
    args=(spec['from'],spec['to'])
    values=[db.execute("SELECT o.oid,o.day,o.store,o.current,r.digest FROM normal_orders o JOIN normal_revisions r ON r.oid=o.oid AND r.revision=o.current WHERE o.day>=? AND o.day<=? AND o.kind NOT IN ('material','policy','opening') ORDER BY o.oid",args).fetchall()]
    for table,columns,order in [('lifecycle_versions','oid,revision,warehouse,stage,version,digest','oid,revision,warehouse,stage,version'),
                               ('archive_visibility','oid,warehouse,deleted,token','oid,warehouse'),
                               ('merchant_feedback','oid,revision,version,payload','oid,revision,version'),
                               ('lifecycle_routes','oid,revision,version','oid,revision'),
                               ('jst_active','oid,warehouse,gid','oid,warehouse'),
                               ('jst_generations','oid,revision,warehouse,gid,digest','oid,warehouse,gid'),
                               ('jst_events','oid,warehouse,id,action,at','oid,warehouse,id'),
                               ('warehouse_execution_heads','oid,warehouse,source_revision,child_revision,state,epoch','oid,warehouse'),
                               ('warehouse_execution_revisions','oid,warehouse,source_revision,identity_digest,snapshot_digest,saved','oid,warehouse,source_revision')]:
        if table.startswith('warehouse_execution_') and table not in tables:continue
        relevant=" AND action IN ('生成聚水潭订单','库存不足未生成','撤销未下单生成单')" if table=='jst_events' else ''
        values.append(db.execute(f'SELECT {columns} FROM {table} WHERE oid IN ({selection})'+relevant+f' ORDER BY {order}',args).fetchall() if table in tables else [])
    if not spec.get('as_of'):
        from jst_shipping import read as read_shipments
        shipments=read_shipments(db,(record[0] for record in values[0]))
        if shipments:values.append(sorted((*key, shipment['payment_lock']) for key,shipment in shipments.items()))
    return digest(values)


def _require_basis(path,db,report,cancel):
    if data_stamp(db,report['query'])!=report['data_stamp']:
        raise ValueError('订单或履约数据已更新，请重新查询；原报告数据未混入本次结果')
    from analytics_identifiers import token
    if token(path,db,report['heads'],cancel)!=report.get('identifier_token'):
        raise ValueError('订单关联单号已更新，请重新查询；原报告中的单号未自动替换')


def validate_basis(path,report,progress=lambda _:None,cancel=None):
    checkpoint(cancel)
    if report['query'].get('as_of'):return validate_snapshot(path,report,progress,cancel)
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('BEGIN');_require_basis(path,db,report,cancel)
    checkpoint(cancel)


_ANALYSIS_FIELDS=(('work_revision',),('new_focus',),('result','rows'),('result','store_name'),
                  ('result','must_list'),('result','must_list_audit'),('model_version',),
                  ('result','version','model_version'))
_WAREHOUSES=('山东仓','河北仓')


def _order_records(db,spec):
    """Read the signed identity index before fetching any snapshot payload."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='normal_orders'").fetchone():return []
    scope="o.day>=? AND o.day<=? AND o.kind NOT IN ('material','policy','opening')";args=[spec['from'],spec['to']]
    if spec['stores']:
        scope+=' AND o.store IN (SELECT value FROM json_each(?))';args.append(json.dumps(spec['stores']))
    where='o.current=r.revision';at=spec.get('as_of')
    if at:where='r.revision=(SELECT max(x.revision) FROM normal_revisions x WHERE x.oid=o.oid AND julianday(x.saved)<=julianday(?))'
    records=db.execute('SELECT o.day,o.store,o.oid,o.number,r.revision,r.digest,r.saved FROM normal_orders o JOIN normal_revisions r ON o.oid=r.oid AND '+where+' WHERE '+scope+' ORDER BY o.day DESC,o.store',([at] if at else [])+args).fetchall()
    managed=set()
    if execution.exists(db):
        managed_parents=db.execute('SELECT o.day,o.store,o.oid,o.number FROM normal_orders o WHERE '+scope+' AND EXISTS(SELECT 1 FROM warehouse_execution_heads h WHERE h.oid=o.oid)',args).fetchall()
        managed={p[2] for p in managed_parents};present={r[2] for r in records}
        # A historical child remains readable even if its shared parent has no revision before the cutoff.
        records.extend((*p,None,None,None) for p in managed_parents if p[2] not in present)
        records.sort(key=lambda r:r[1]);records.sort(key=lambda r:r[0],reverse=True)
    numbers=set(spec['orders'])
    if numbers:
        records=[r for r in records if any(ao.number(r[3],wh) in numbers for wh in _WAREHOUSES)]
    selected=({'oid':r[2]} for r in records if r[2] in managed)
    child_heads=execution.heads(db,selected,history=bool(at),with_names=False) if managed else []
    if at:
        chosen={}
        for head in child_heads:
            if db.execute('SELECT julianday(?)<=julianday(?)',(head['saved'],at)).fetchone()[0]:chosen.setdefault((head['oid'],head['warehouse']),head)
        child_heads=list(chosen.values())
    by_parent={}
    for head in child_heads:by_parent.setdefault(head['oid'],[]).append(head)
    return [(record,head) for record in records for head in (by_parent.get(record[2],[]) if record[2] in managed else [None])]


def _lifecycle_documents(db,head,at,cancel):
    from merchant_feedback import current as old_feedback
    route,docs=lifecycle.read_documents(db,head,cancel,as_of=at)
    return route,docs,old_feedback(db,head['oid'],head['revision'],as_of=at)


def _query_orders(path,db,spec,progress,cancel,seen_stores,seen_orders,*,mode,verifying):
    from history_read import repository
    from jst_shipping import read as read_shipments
    records=_order_records(db,spec);visibility=ao.states(db);numbers=set(spec['orders'])
    at=spec.get('as_of');stage=spec.get('lifecycle_from','')
    shipments=read_shipments(db,(r[2] for r,_ in records),cancel) if not at else {}
    reader=repository(path)
    for i,(record,signed_head) in enumerate(records,1):
        checkpoint(cancel)
        day,store,oid,number,revision,sha,saved=record
        candidates=(signed_head['warehouse'],) if signed_head else _WAREHOUSES
        allowed={wh:ao.number(number,wh) for wh in candidates if wh in _WAREHOUSES
                 and (not spec.get('warehouse') or spec['warehouse']==wh)
                 and not visibility.get((oid,wh),{}).get('deleted')}
        allowed={wh:child for wh,child in allowed.items() if not numbers or child in numbers}
        if not allowed:continue
        basis=None;view=None
        if signed_head and stage:
            # Independent warehouse heads establish scope without restoring the signed result.
            # Legacy merchant feedback needs the original signed product set, so defer its decision.
            wh=signed_head['warehouse'];seen_orders.add(allowed[wh]);seen_stores.add(store)
            basis=_lifecycle_documents(db,signed_head,at,cancel)
            route,docs,old=basis
            if not old:
                view=lifecycle.snapshot_state(signed_head,route,docs,None,shipment=shipments.get((oid,signed_head['revision'],wh)))
                if not lifecycle_scope.includes(view,stage):continue
        progress(RowProgress('读取签发与履约快照',i,len(records)))
        if signed_head:
            kwargs={'fields':_ANALYSIS_FIELDS,'progress':progress} if mode!='full' else {'progress':progress,'export_io':True} if verifying else {}
            order=execution.get_order(reader,signed_head,db=db,**kwargs)
            payload=order['payload'];order['name']=payload['result'].get('store_name',store)
        else:
            blob=db.execute('SELECT payload FROM normal_revisions WHERE oid=? AND revision=?',(oid,revision)).fetchone()[0]
            payload=chunks.load_fields(blob,db,_ANALYSIS_FIELDS,binding=sha,progress=progress) if mode!='full' else chunks.load(blob,db,binding=sha,**({'bounded':True} if verifying else {}))
            order={'day':day,'store':store,'oid':oid,'number':number,'revision':revision,'current_revision':revision,'digest':sha,'saved':saved,'payload':payload}
        result=payload['result']
        for wh in dict.fromkeys(ao.warehouse(r) for r in result['rows']):
            if wh not in allowed:continue
            h={k:v for k,v in order.items() if k!='payload'}
            h.update(number=allowed[wh],parent_number=number,warehouse=wh,visibility_token=visibility.get((oid,wh),{}).get('token',0))
            seen_stores.add(store);seen_orders.add(h['number'])
            if view is None:
                route,docs,old=basis or _lifecycle_documents(db,h,at,cancel)
                legacy=None
                if old:
                    products={r['E']:{'quantity':sem.order_quantity(r)} for group in sem.signed_groups(result,payload.get('new_focus')).values() for r in group if ao.warehouse(r)==wh}
                    legacy=lifecycle.legacy_return_record(old,products)
                current=lifecycle.snapshot_state(h,route,docs,legacy,shipment=shipments.get((oid,h['revision'],wh)))
            else:current={**view,'head':h}
            if lifecycle_scope.includes(current,stage):yield order,h,current
        order=payload=result=current=basis=view=route=docs=old=None


def collect(path, spec, progress=lambda _:None, cancel=None, *, _coverage=True, _verification=None, _mode='full', _anchor=None):
    from analytics_identifiers import local, attach
    spec=query_spec(spec);path=Path(path).resolve();verifying=_verification is not None
    rows=_verification if verifying else [];tokens=[];heads=[];payments=[];identifiers=[]
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('BEGIN')
        if _anchor is not None:_require_basis(path,db,_anchor,cancel)
        stamp=data_stamp(db,spec)
        import workflow_analysis as workflow
        allocations=workflow.AllocationReader(db,spec,cancel)
        show_allocation_detail = _mode in ('full', 'analysis_detail')
        evidence=Evidence(db,cancel);seen_stores=set();seen_orders=set()
        for order,h,view in _query_orders(path,db,spec,progress,cancel,seen_stores,seen_orders,mode=_mode,verifying=verifying):
            day,store,wh,child=(h[k] for k in ('day','store','warehouse','number'))
            allocation,needs_reallocation=workflow.allocation_fact(db,h,view,spec.get('as_of'),event=allocations.event(h))
            saved_rows = workflow.allocation_detail_rows(db, h, view, allocation, spec.get('as_of')) if show_allocation_detail else {}
            payment=view['documents']['payment']
            references=local(db,h,payment)
            identifiers.append({'day':day,'store':store,'store_name':order['payload']['result']['store_name'],'warehouse':wh,'order':child,'references':references})
            if payment:
                totals={}
                for raw in payment['payload'].get('raw_rows',payment['payload']['rows']):
                    currency=raw.get('currency') or '未标币种';entry=totals.setdefault(currency,{'amount':Decimal(0),'missing':0});value=num(raw.get('amount'))
                    if value is None:entry['missing']+=1
                    else:entry['amount']+=value
                state,valid=comparison.stage_state(view,'payment')
                payments.extend({'order':child,'store':store,'warehouse':wh,'currency':c,**v,'state':state,'valid':valid} for c,v in totals.items())
            tokens.append([h,[(s,d['version'],d['digest']) if d else [s,None] for s,d in view['documents'].items()],view.get('legacy_return'),allocation['digest'] if allocation else None])
            heads.append(h)
            for row in workflow.enrich(build_facts(order,h,view,evidence,cancel,spec=spec,mode=_mode),h,view,allocation,h['saved'],
                                       needs_reallocation=needs_reallocation and not saved_rows):
                saved_row = saved_rows.get(row['sku'])
                if saved_row is not None:
                    row['allocation_detail'] = {k: saved_row.get(k) for k in ('stock', 'before', 'quantity', 'reason')}
                row['references']=references;rows.append(row)
            order=view=allocation=payment=row=saved_rows=saved_row=None
        if set(spec['stores'])-seen_stores:raise ValueError('所选门店在日期和仓范围内没有有效签发：'+'、'.join(sorted(set(spec['stores'])-seen_stores)))
        if set(spec['orders'])-seen_orders:raise ValueError('所选订单不存在、已删除或不在查询范围，请重新查询')
        identifier_token=attach(path,db,heads,identifiers,cancel)
    checkpoint(cancel)
    from analytics_snapshot import snapshot_digest
    snapshot_id=snapshot_digest(VERSION,spec,tokens,rows,payments,identifiers,cancel)
    if verifying:return {'snapshot_id':snapshot_id}
    report={'schema':VERSION,'query':spec,'as_of':spec.get('as_of') or datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds'),
            'snapshot_id':snapshot_id,'data_stamp':stamp,'identifier_token':identifier_token,
            'heads':heads,'rows':rows,'payments':payments,'identifiers':identifiers}
    if _mode!='full':report['_projection']=_mode
    if _mode not in ('overview','full'):return report
    progress('汇总符合条件的仓单和漏斗指标')
    report.update(metrics=aggregate(rows),groups={g:aggregate([r for r in rows if r['group']==g]) for g in GROUPS},
                  stores=summaries(rows,'store'),warehouses=summaries(rows,'warehouse'),orders=summaries(rows,'order'))
    if _mode=='overview':return report
    from history_diagnostics import review
    report['history']=review(report,spec.get('rounds',8))
    report['workflow']={mode:workflow.summary(rows,mode) for mode in workflow.METRICS}
    if _coverage:
        from coverage_metrics import projection
        progress('计算预计可售天数，固定当时预测依据…')
        report['coverage']=projection(rows,spec.get('coverage_through','signed'),cancel)
    return report


def collect_overview(path,spec,progress=lambda _:None,cancel=None):
    # A historical cutoff uses the existing full historical verification path.
    mode='full' if spec.get('as_of') else 'overview'
    return collect(path,spec,progress,cancel,_mode=mode)


def collect_view(path,overview,mode,orders=None,progress=lambda _:None,cancel=None):
    """Load an expanded view of the successful overview's exact order basis."""
    from itertools import zip_longest
    spec=dict(overview['query']);selected=None if orders is None else set(orders)
    if selected is not None:
        available={h['number']:h['store'] for h in overview['heads']}
        if not selected or selected-available.keys():raise ValueError('勾选订单不在当前查询范围')
        spec['orders']=list(dict.fromkeys(orders))
        stores={available[number] for number in selected}
        spec['stores']=[store for store in spec['stores'] if store in stores]
    result=collect(path,spec,progress,cancel,_mode=mode,_anchor=overview)
    expected=(row for row in overview['rows'] if selected is None or row['order'] in selected)
    if mode=='analysis_detail':
        from analytics_template_export import retain_detail_row
        expected=filter(retain_detail_row,expected)
    elif mode=='detail':expected=(row for row in expected if row['scope']!='排除')
    for before,after in zip_longest(expected,result['rows']):
        checkpoint(cancel)
        if before is None or after is None or any(after.get(k)!=v for k,v in before.items()):
            raise ValueError('展开的明细与原查询依据不一致，请重新查询')
    return result


def calculate_view(report,mode,progress=lambda _:None,cancel=None):
    if mode=='coverage':
        from coverage_metrics import projection
        progress('计算当前查询范围的预计可售天数')
        return projection(report['rows'],report['query'].get('coverage_through','signed'),cancel)
    from history_diagnostics import review
    progress('整理当前查询范围的多轮复盘')
    checkpoint(cancel);result=review(report,report['query'].get('rounds',8));checkpoint(cancel)
    return result


def validate_snapshot(path, report, progress=lambda _:None,cancel=None,*,temp_folder=None):
    from analytics_snapshot import FactSpool
    with FactSpool(temp_folder,cancel,progress) as spool:
        current=collect(path,report['query'],progress,cancel,_coverage=False,_verification=spool,_mode=report.get('_projection','full'))
    if current['snapshot_id']!=report['snapshot_id']:
        raise ValueError('分析依据或订单范围已变化，请刷新后重新导出；未导出旧缓存')
    return True
