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
from runtime_support import checkpoint
from export_progress import RowProgress

VERSION = 'funnel-1'
STAGES = ('model', 'signed', 'return', 'payment', 'receipt')
NAMES = dict(model='模型建议', signed='签发', **{'return':'门店确认'}, payment='实际下单', receipt='实际收货')
GROUPS = ('必采', '非必采', '属性待核')
PAIRS = tuple(zip(STAGES, STAGES[1:]))
# Exact tokens observed in the two original warehouse exports, not fuzzy substrings.
RETIRED = frozenset(['标杆淘汰'] + ['标杆淘汰（'+x+'）' for x in (
    '箱规废弃','厂家停产','夏季','冬季','拉回下单','样品/赠品','质量问题','节庆商品','召回退供','河北仓专属')])
num = comparison.num
amount = comparison.amount


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False, default=str).encode()).hexdigest()


def query_spec(spec):
    allowed = {'from','to','stores','orders','warehouse','group','search','category','rounds','coverage_through'}
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

    def original(self, catalog):
        sha, sheet = catalog.get('source_sha256'), catalog.get('sheet')
        key = (sha, sheet)
        if key not in self.sources:
            record = source_archive.read(self.db, sha) if sha else None
            result = {}
            if record:
                import openpyxl
                from input_adapter import identifier
                book = openpyxl.load_workbook(io.BytesIO(record[1]),read_only=True,data_only=True,keep_links=False)
                try:
                    if sheet not in book.sheetnames: raise ValueError('当时仓资料工作表不一致')
                    ws=book[sheet];ws.reset_dimensions();it=ws.iter_rows(values_only=True)
                    heads=[str(v).strip() if v is not None else '' for v in next(it,())]
                    fields=('商品编码','商品状态','虚拟分类')
                    if all(heads.count(k)==1 for k in fields):
                        indexes={k:heads.index(k) for k in fields}
                        for rownum, values in enumerate(it,2):
                            checkpoint(self.cancel)
                            raw={k:values[i] if i<len(values) else None for k,i in indexes.items()}
                            sku,error=identifier(raw['商品编码'])
                            if error: continue
                            result[sku] = None if sku in result else {**raw,'source_row':rownum}
                finally: book.close()
            self.sources[key]=result
            if len(self.sources)>4:self.sources.popitem(last=False)
        self.sources.move_to_end(key)
        return self.sources[key]

    def item(self, order, warehouse, sku):
        c=self.catalog(order,warehouse)
        row=c.get('items',{}).get(sku)
        if row and '虚拟分类' in row: return row, c.get('revision')
        return self.original(c).get(sku), c.get('source_sha256')


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
    skus=list(bysku)
    for s in lifecycle.STAGES: skus.extend(sku for sku in maps[s] if sku not in skus)
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
        name=r.get('D') if r else next((maps[s][sku].get('name','') for s in lifecycle.STAGES if sku in maps[s]),'')
        rows.append({'day':head['day'],'store':head['store'],'store_name':result['store_name'],
            'warehouse':head['warehouse'],'order':head['number'],'sku':sku,'name':name,'unit':r.get('G') if r else None,
            'category':r.get('U') if r else None,'group':membership(result,r,sku),'scope':scope,'reasons':reasons,
            'price':price,'quantities':quantities,'valid':{s:bool(v[1]) and quantities[s] is not None for s,v in states.items()},
            'states':{s:v[0] for s,v in states.items()},'master_basis':basis,
            'source_row':r.get('_meta',{}).get('export_row') if r else None,
            'observations':{k:r.get(k) for k in ('H','I','J','K','L','N','AN','AM','AG','AH','BD','AL')} if r else {},
            'decision':{'budget':r.get('_budget_reason'),'manual':r.get('_preissue',{}).get('reason'),
                        'new':r.get('_new_requested'),'one_box':r.get('_audit',{}).get('applied')} if r else {},
            'payment_amount':num(maps['payment'].get(sku,{}).get('amount')),
            'payment_currency':maps['payment'].get(sku,{}).get('currency') or '未标币种',
            'payment_source':view['documents']['payment']['payload'].get('source_kind','原付款单据') if view['documents']['payment'] else None,
            'notes':{s:maps[s].get(sku,{}).get('processing_note','') for s in lifecycle.STAGES}})
    return rows


def aggregate(rows):
    """Ratios always use the same priced valid row cohort in both stages."""
    eligible=[r for r in rows if r['scope']=='纳入']
    out={'rows':len(rows),'included':len(eligible),'included_orders':len({r['order'] for r in eligible}),
         'complete_orders':sum(all(all(r['valid'].values()) for r in eligible if r['order']==oid) for oid in {r['order'] for r in eligible}),
         'complete_rows':sum(all(r['valid'].values()) for r in eligible),'excluded':sum(r['scope']=='排除' for r in rows),
         'pending':sum(r['scope']=='待核' for r in rows),'stages':{},'pairs':[]}
    for s in STAGES:
        valid=[r for r in eligible if r['valid'][s]]
        values=[amount(r['quantities'][s],r['price']) for r in valid]
        out['stages'][s]={'value':sum((v for v in values if v is not None),Decimal(0)) if valid else None,
            'valid_rows':len(valid),'unpriced':sum(v is None for v in values),
            'skus':len({r['sku'] for r in valid if r['quantities'][s]>0}),
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
    groups=OrderedDict()
    for r in rows:
        key=(r['store'],r['warehouse'],r['order']) if level=='order' else (r['store'],r['warehouse']) if level=='warehouse' else (r['store'],)
        groups.setdefault(key,[]).append(r)
    return [{'key':key,'store':rs[0]['store'],'name':rs[0]['store_name'],
             'warehouse':rs[0]['warehouse'] if level!='store' else ' / '.join(dict.fromkeys(r['warehouse'] for r in rs)),
             'order':rs[0]['order'] if level=='order' else '',
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
                               ('merchant_feedback','oid,revision,version,payload','oid,revision,version')]:
        values.append(db.execute(f'SELECT {columns} FROM {table} WHERE oid IN ({selection}) ORDER BY {order}',args).fetchall() if table in tables else [])
    return digest(values)


def validate_basis(path,report,progress=lambda _:None,cancel=None):
    checkpoint(cancel)
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('BEGIN')
        if data_stamp(db,report['query'])!=report['data_stamp']:
            raise ValueError('订单或履约数据已更新，请重新查询；原报告数据未混入本次结果')
    checkpoint(cancel)


def collect(path, spec, progress=lambda _:None, cancel=None, *, _coverage=True):
    spec=query_spec(spec);path=Path(path).resolve();rows=[];tokens=[];heads=[];payments=[]
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('BEGIN')
        stamp=data_stamp(db,spec)
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='normal_orders'").fetchone():
            records=[]
        else:
            records=db.execute("SELECT o.day,o.store,o.oid,o.number,o.current,r.digest,r.saved,r.payload FROM normal_orders o JOIN normal_revisions r ON o.oid=r.oid AND o.current=r.revision WHERE o.day>=? AND o.day<=? ORDER BY o.day DESC,o.store",(spec['from'],spec['to'])).fetchall()
        visibility=ao.states(db);evidence=Evidence(db,cancel);seen_stores=set();seen_orders=set()
        for i,(day,store,oid,number,revision,sha,saved,blob) in enumerate(records):
            checkpoint(cancel)
            if spec['stores'] and store not in spec['stores']:continue
            progress(RowProgress('读取签发与履约快照',i+1,len(records)))
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
                view=lifecycle.snapshot(path,h,db=db,order=order,cancel=cancel)
                payment=view['documents']['payment']
                if payment:
                    totals={}
                    for raw in payment['payload'].get('raw_rows',payment['payload']['rows']):
                        currency=raw.get('currency') or '未标币种';entry=totals.setdefault(currency,{'amount':Decimal(0),'missing':0});value=num(raw.get('amount'))
                        if value is None:entry['missing']+=1
                        else:entry['amount']+=value
                    from lifecycle_comparison import stage_state
                    state,valid=stage_state(view,'payment')
                    payments.extend({'order':child,'store':store,'warehouse':wh,'currency':c,**v,'state':state,'valid':valid} for c,v in totals.items())
                tokens.append([h,[(s,d['version'],d['digest']) if d else [s,None] for s,d in view['documents'].items()],view.get('legacy_return')])
                heads.append(h);seen_stores.add(store);seen_orders.add(child)
                for row in build_facts(order,h,view,evidence,cancel):
                    if spec.get('group') and row['group']!=spec['group']:continue
                    if spec.get('category') and row['category']!=spec['category']:continue
                    terms=spec.get('search','').casefold().split()
                    if not all(t in (row['store_name']+' '+row['order']+' '+row['sku']+' '+str(row['name'])).casefold() for t in terms):continue
                    rows.append(row)
        if set(spec['stores'])-seen_stores:raise ValueError('所选门店在日期和仓范围内没有有效签发：'+'、'.join(sorted(set(spec['stores'])-seen_stores)))
        if set(spec['orders'])-seen_orders:raise ValueError('所选订单不存在、已删除或不在查询范围，请重新查询')
    checkpoint(cancel)
    report={'schema':VERSION,'query':spec,'as_of':datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds'),
            'snapshot_id':digest([VERSION,spec,tokens,rows,payments]),'data_stamp':stamp,'heads':heads,'rows':rows,'payments':payments,
            'metrics':aggregate(rows),'groups':{g:aggregate([r for r in rows if r['group']==g]) for g in GROUPS},
            'stores':summaries(rows,'store'),'warehouses':summaries(rows,'warehouse'),'orders':summaries(rows,'order')}
    from history_diagnostics import review
    report['history']=review(report,spec.get('rounds',8))
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
