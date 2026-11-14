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
from export_progress import RowProgress

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
    allowed = {'from','to','stores','orders','warehouse','group','search','category','rounds','coverage_through','as_of'}
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
        target=num(row.get('BF'))
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


def membership(result, row, sku):
    listing=result.get('must_list')
    if not listing or not listing.get('revision'): return '属性待核'
    if row is not None and type(row.get('_must_sell')) is bool:
        return '必采' if row['_must_sell'] else '非必采'
    audit=result.get('must_list_audit')
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


def build_facts(order, head, view, evidence, cancel=None):
    result=order['payload']['result']
    raw=[r for r in result['rows'] if ao.warehouse(r)==head['warehouse']]
    bysku={r['E']:r for r in raw}
    if len(bysku)!=len(raw): raise ValueError('签发快照商品编码重复，不能合计')
    maps={s:{r['sku']:r for r in d['payload']['rows']} if d else {} for s,d in view['documents'].items()}
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
    rows=[]
    for sku in skus:
        checkpoint(cancel);r=bysku.get(sku);master,basis=evidence.item(order,head['warehouse'],sku)
        scope,reasons=inclusion(r,master)
        quantities={'model':num(r.get('AS')) if r else None,'signed':num(sem.order_quantity(r)) if r else Decimal(0)}
        for s in lifecycle.STAGES:
            doc=view['documents'][s]
            if sku in maps[s]: quantities[s]=num(maps[s][sku].get('base_quantity'))
            elif doc and doc['payload'].get('schema')==lifecycle.SCHEMA: quantities[s]=Decimal(0)
            else: quantities[s]=None
        price=num(r.get('V')) if r else None
        name=r.get('D') if r else next((maps[s][sku].get('name','') for s in lifecycle.ALL_STAGES if sku in maps.get(s,{})),'')
        rows.append({'day':head['day'],'store':head['store'],'store_name':result['store_name'],
            'warehouse':head['warehouse'],'order':head['number'],'sku':sku,'name':name,'unit':r.get('G') if r else None,
            'order_status':view['status'],'order_next':view['next'],
            'category':r.get('U') if r else None,'group':membership(result,r,sku),'scope':scope,'reasons':reasons,
            'price':price,'quantities':quantities,'valid':{s:bool(v[1]) and quantities[s] is not None for s,v in states.items()},
            'states':{s:v[0] for s,v in states.items()},'master_basis':basis,
            'source_row':r.get('_meta',{}).get('export_row') if r else None,
            'observations':{k:r.get(k) for k in ('H','I','J','K','L','N','AN','AM','AG','AH','BD','AL')} if r else {},
            'template_attributes':evidence.template_attributes(order,head['warehouse'],sku,r),
            'decision':{'budget':r.get('_budget_reason'),'manual':r.get('_preissue',{}).get('reason'),
                        'new':r.get('_new_requested'),'one_box':r.get('_audit',{}).get('applied')} if r else {},
            'payment_amount':num(maps['payment'].get(sku,{}).get('amount')),
            'payment_currency':maps['payment'].get(sku,{}).get('currency') or '未标币种',
            'payment_source':view['documents']['payment']['payload'].get('source_kind','原付款单据') if view['documents']['payment'] else None,
            'notes':{s:maps[s].get(sku,{}).get('processing_note','') for s in lifecycle.STAGES},
            **evidence.dimensions(order,head['warehouse'],sku,r)})
    return rows


def aggregate(rows):
    """Ratios always use the same priced valid row cohort in both stages."""
    eligible=[r for r in rows if r['scope']=='纳入']
    out={'rows':len(rows),'included':len(eligible),'included_orders':len({r['order'] for r in eligible}),
         'complete_orders':sum(all(all(r['valid'][s] for s in STAGES) for r in eligible if r['order']==oid) for oid in {r['order'] for r in eligible}),
         'complete_rows':sum(all(r['valid'][s] for s in STAGES) for r in eligible),'excluded':sum(r['scope']=='排除' for r in rows),
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
        if r['valid']['payment']:
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
    selection='SELECT oid FROM normal_orders WHERE day>=? AND day<=?'
    args=(spec['from'],spec['to'])
    values=[db.execute('SELECT o.oid,o.day,o.store,o.current,r.digest FROM normal_orders o JOIN normal_revisions r ON r.oid=o.oid AND r.revision=o.current WHERE o.day>=? AND o.day<=? ORDER BY o.oid',args).fetchall()]
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
    return digest(values)


def validate_basis(path,report,progress=lambda _:None,cancel=None):
    checkpoint(cancel)
    if report['query'].get('as_of'):return validate_snapshot(path,report,progress,cancel)
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('BEGIN')
        if data_stamp(db,report['query'])!=report['data_stamp']:
            raise ValueError('订单或履约数据已更新，请重新查询；原报告数据未混入本次结果')
        from analytics_identifiers import token
        if token(path,db,report['heads'],cancel)!=report.get('identifier_token'):
            raise ValueError('订单关联单号已更新，请重新查询；原报告中的单号未自动替换')
    checkpoint(cancel)


def collect(path, spec, progress=lambda _:None, cancel=None, *, _coverage=True):
    from analytics_identifiers import local, attach
    spec=query_spec(spec);path=Path(path).resolve();rows=[];tokens=[];heads=[];payments=[];identifiers=[]
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('BEGIN')
        stamp=data_stamp(db,spec)
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='normal_orders'").fetchone():
            records=[]
        else:
            where='o.current=r.revision';args=[spec['from'],spec['to']]
            if spec.get('as_of'):
                where='r.revision=(SELECT max(x.revision) FROM normal_revisions x WHERE x.oid=o.oid AND julianday(x.saved)<=julianday(?))'
                args.insert(0,spec['as_of'])
            records=db.execute('SELECT o.day,o.store,o.oid,o.number,r.revision,r.digest,r.saved,r.payload FROM normal_orders o JOIN normal_revisions r ON o.oid=r.oid AND '+where+' WHERE o.day>=? AND o.day<=? ORDER BY o.day DESC,o.store',args).fetchall()
        managed=set()
        if execution.exists(db):
            managed_parents=db.execute("SELECT day,store,oid,number FROM normal_orders o WHERE day>=? AND day<=? AND EXISTS(SELECT 1 FROM warehouse_execution_heads h WHERE h.oid=o.oid) ORDER BY day DESC,store",(spec['from'],spec['to'])).fetchall()
            managed={p[2] for p in managed_parents};present={r[2] for r in records}
            # Child evidence survives parent revision replacement/pruning. In
            # particular, an as-of query must not discard an older active child
            # merely because no shared parent revision precedes the cutoff.
            records.extend((*p,None,None,None,None) for p in managed_parents if p[2] not in present)
            records.sort(key=lambda r:r[1]);records.sort(key=lambda r:r[0],reverse=True)
        child_heads=execution.heads(db,[{'oid':oid} for oid in managed],history=bool(spec.get('as_of'))) if managed else []
        if spec.get('as_of'):
            chosen={}
            for head in child_heads:
                if db.execute('SELECT julianday(?)<=julianday(?)',(head['saved'],spec['as_of'])).fetchone()[0]:chosen.setdefault((head['oid'],head['warehouse']),head)
            child_heads=list(chosen.values())
        by_parent={}
        for head in child_heads:by_parent.setdefault(head['oid'],[]).append(head)
        records=[(record,head) for record in records for head in (by_parent.get(record[2],[]) if record[2] in managed else [None])]
        import workflow_analysis as workflow
        allocations=workflow.AllocationReader(db,spec,cancel)
        visibility=ao.states(db);evidence=Evidence(db,cancel);seen_stores=set();seen_orders=set()
        for i,(record,execution_head) in enumerate(records):
            day,store,oid,number,revision,sha,saved,blob=record
            checkpoint(cancel)
            if spec['stores'] and store not in spec['stores']:continue
            progress(RowProgress('读取签发与履约快照',i+1,len(records)))
            if execution_head:
                from history_read import repository
                order=execution.get_order(repository(path),execution_head,db=db)
                revision,sha,saved=(order[k] for k in ('revision','digest','saved'));payload=order['payload']
            else:
                payload=chunks.load(blob,db,binding=sha)
                order={'day':day,'store':store,'oid':oid,'number':number,'revision':revision,'current_revision':revision,'digest':sha,'saved':saved,'payload':payload}
            result=payload['result']
            for wh in dict.fromkeys(ao.warehouse(r) for r in result['rows']):
                if wh not in ('山东仓','河北仓') or visibility.get((oid,wh),{}).get('deleted'):continue
                if spec.get('warehouse') and wh!=spec['warehouse']:continue
                child=ao.number(number,wh)
                if spec['orders'] and child not in spec['orders']:continue
                h={k:v for k,v in order.items() if k!='payload'}
                h.update(number=child,parent_number=number,warehouse=wh,visibility_token=visibility.get((oid,wh),{}).get('token',0))
                view=lifecycle.snapshot(path,h,db=db,order=order,cancel=cancel,as_of=spec.get('as_of'))
                allocation=workflow.allocation_fact(db,h,view,spec.get('as_of'),event=allocations.event(h))
                payment=view['documents']['payment']
                references=local(db,h,payment)
                identifiers.append({'day':day,'store':store,'store_name':result['store_name'],'warehouse':wh,'order':child,'references':references})
                if payment:
                    totals={}
                    for raw in payment['payload'].get('raw_rows',payment['payload']['rows']):
                        currency=raw.get('currency') or '未标币种';entry=totals.setdefault(currency,{'amount':Decimal(0),'missing':0});value=num(raw.get('amount'))
                        if value is None:entry['missing']+=1
                        else:entry['amount']+=value
                    from lifecycle_comparison import stage_state
                    state,valid=stage_state(view,'payment')
                    payments.extend({'order':child,'store':store,'warehouse':wh,'currency':c,**v,'state':state,'valid':valid} for c,v in totals.items())
                tokens.append([h,[(s,d['version'],d['digest']) if d else [s,None] for s,d in view['documents'].items()],view.get('legacy_return'),allocation['digest'] if allocation else None])
                heads.append(h);seen_stores.add(store);seen_orders.add(child)
                for row in workflow.enrich(build_facts(order,h,view,evidence,cancel),h,view,allocation,saved):
                    row['references']=references
                    if spec.get('group') and row['group']!=spec['group']:continue
                    if spec.get('category') and row['category']!=spec['category']:continue
                    terms=spec.get('search','').casefold().split()
                    if not all(t in (row['store_name']+' '+row['order']+' '+row['sku']+' '+str(row['name'])).casefold() for t in terms):continue
                    rows.append(row)
        if set(spec['stores'])-seen_stores:raise ValueError('所选门店在日期和仓范围内没有有效签发：'+'、'.join(sorted(set(spec['stores'])-seen_stores)))
        if set(spec['orders'])-seen_orders:raise ValueError('所选订单不存在、已删除或不在查询范围，请重新查询')
        identifier_token=attach(path,db,heads,identifiers,cancel)
    checkpoint(cancel)
    report={'schema':VERSION,'query':spec,'as_of':spec.get('as_of') or datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds'),
            'snapshot_id':digest([VERSION,spec,tokens,rows,payments,identifiers]),'data_stamp':stamp,'identifier_token':identifier_token,
            'heads':heads,'rows':rows,'payments':payments,'identifiers':identifiers,
            'metrics':aggregate(rows),'groups':{g:aggregate([r for r in rows if r['group']==g]) for g in GROUPS},
            'stores':summaries(rows,'store'),'warehouses':summaries(rows,'warehouse'),'orders':summaries(rows,'order')}
    from history_diagnostics import review
    report['history']=review(report,spec.get('rounds',8))
    report['workflow']={mode:workflow.summary(rows,mode) for mode in workflow.METRICS}
    if _coverage:
        from coverage_metrics import projection
        progress('计算预计可售天数，固定当时预测依据…')
        report['coverage']=projection(rows,spec.get('coverage_through','signed'),cancel)
    return report


def validate_snapshot(path, report, progress=lambda _:None,cancel=None):
    current=collect(path,report['query'],progress,cancel,_coverage=False)
    if current['snapshot_id']!=report['snapshot_id']:
        raise ValueError('分析依据或订单范围已变化，请刷新后重新导出；未导出旧缓存')
    return True
