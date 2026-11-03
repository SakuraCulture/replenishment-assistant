"""Read-only order/store lifecycle analytics using signed prices and exact document facts."""
from collections import OrderedDict
from contextlib import closing
from decimal import Decimal,InvalidOperation
from datetime import datetime
from pathlib import Path
import os,sqlite3,tempfile
import archive_orders as ao,order_lifecycle as lc,purchases,normal_exports as ne
from history_read import repository
from runtime_support import checkpoint
from export_progress import RowProgress

STAGES=('signed',*lc.STAGES)
NAMES={'signed':'签发','return':'回传','payment':'付款','receipt':'收货'}
PAIRS=(('signed','return'),('return','payment'),('payment','receipt'))
PAIR_NAMES=('回传−签发','付款−回传','收货−付款')
IDENTITY=['业务日期','门店编码','门店名称','仓','补货单号']
ORDER_HEADERS=IDENTITY+['当前进度']+[x for s in STAGES for x in (NAMES[s]+'状态',NAMES[s]+'商品款数',NAMES[s]+'已计价货值（签发价）')]+[x for n in PAIR_NAMES for x in (n+'货值差额',n+'差异商品款数')]+['付款实际金额（源单）','付款币种','资料提示']
STORE_HEADERS=['门店编码','门店名称','所含仓','最早业务日期','最晚业务日期','订单数']+[x for s in STAGES for x in (NAMES[s]+'有效订单数',NAMES[s]+'商品款数（去重）',NAMES[s]+'已计价货值（签发价）')]+[x for n in PAIR_NAMES for x in (n+'可比订单数',n+'货值差额',n+'差异商品款数（去重）')]+['付款实际金额（有效单）','付款币种','资料提示']
DETAIL_HEADERS=IDENTITY+['商品编码','商品名称','库存单位','签发时单价']+[NAMES[s]+'数量' for s in STAGES]+[n+'数量差额' for n in PAIR_NAMES]+[n+'货值差额' for n in PAIR_NAMES]+[NAMES[s]+'状态' for s in lc.STAGES]+['资料说明']
PAY_HEADERS=IDENTITY+['付款状态','币种','已知实际付款金额','缺金额行数']

def num(v):
    if v is None or isinstance(v,bool) or v=='':return None
    try:d=Decimal(str(v));return d if d.is_finite() else None
    except (InvalidOperation,ValueError):return None

def amount(q,price):
    return Decimal(0) if q==0 else None if q is None or price is None or price<0 else q*price

def stage_state(view,stage):
    if stage=='signed':return '已签发',True
    if view['health'].get(stage):return '已确认',True
    if view['documents'][stage]:return '待重新核对',False
    if stage=='return' and view.get('legacy_return'):return '旧回传不完整',False
    if view['next'] is None:return '无需此环节',False
    return '未导入',False

def facts(view,order,cancel=None):
    h=view['head'];result=order['payload']['result'];signed={d['E']:d for rs in ne.groups(result,order['payload'].get('new_focus')).values() for d in rs if ao.warehouse(d)==h['warehouse']}
    status={s:stage_state(view,s) for s in STAGES};maps={s:{r['sku']:r for r in d['payload']['rows']} if d else {} for s,d in view['documents'].items()}
    legacy=view.get('legacy_return')
    if not view['documents']['return'] and legacy:maps['return']={sku:{'base_quantity':v} for sku,v in legacy['values'].items()}
    metrics={s:{'state':status[s][0],'valid':status[s][1],'skus':set(),'value':Decimal(0),'unknown_qty':0,'unpriced':0} for s in STAGES}
    pairs=[{'valid':status[a][1] and status[b][1],'value':Decimal(0),'skus':set(),'unknown':0} for a,b in PAIRS]
    prefix=[h['day'],h['store'],result['store_name'],h['warehouse'],h['number']];details=[]
    for raw in view['rows']:
        checkpoint(cancel);sku,name=raw[:2];d=signed.get(sku);price=num(d.get('V')) if d else None;quantities={'signed':num(ne.sem.order_quantity(d)) if d else Decimal(0)}
        for s in lc.STAGES:
            doc=view['documents'][s]
            if sku in maps[s]:q=num(maps[s][sku].get('base_quantity'))
            # A complete schema2 whole-document record contains all positive upstream
            # lines; absent later-only SKUs were not ordered at this earlier stage.
            elif doc and doc['payload'].get('schema')==lc.SCHEMA:q=Decimal(0)
            else:q=None
            quantities[s]=q
        notes=[];qd=[];ad=[]
        for s,m in metrics.items():
            if not m['valid']:continue
            q=quantities[s];a=amount(q,price)
            if q is None:m['unknown_qty']+=1
            elif q>0:m['skus'].add(sku)
            if a is None:
                if q is not None and q!=0:m['unpriced']+=1;notes.append(NAMES[s]+'无签发价，未计货值')
            else:m['value']+=a
        for pair,(a,b) in zip(pairs,PAIRS):
            delta=quantities[b]-quantities[a] if pair['valid'] and quantities[b] is not None and quantities[a] is not None else None
            value=amount(delta,price);qd.append(delta);ad.append(value)
            if not pair['valid']:continue
            if delta is None or value is None:pair['unknown']+=1
            else:pair['value']+=value
            if delta is not None and delta!=0:pair['skus'].add(sku)
        if notes or any(x is not None and x!=0 for x in qd) or any(status[s][1] and quantities[s] is None for s in STAGES) or any(view['documents'][s] and not status[s][1] and sku in maps[s] for s in lc.STAGES):
            details.append([*prefix,sku,name,d.get('G') if d else '原单位未留存',price,*[quantities[s] for s in STAGES],*qd,*ad,*[status[s][0] for s in lc.STAGES],'；'.join([raw[-1],*notes])])
    pay={};doc=view['documents']['payment']
    if doc:
        for row in doc['payload'].get('raw_rows',doc['payload']['rows']):
            currency=row.get('currency') or '未标币种';g=pay.setdefault(currency,{'value':Decimal(0),'missing':0});a=num(row.get('amount'))
            if a is None:g['missing']+=1
            else:g['value']+=a
    return {'head':h,'prefix':prefix,'status':'已结束' if view['status']=='已收齐' else view['status'],'metrics':metrics,'pairs':pairs,'details':details,'payment':pay,'note':view.get('note','')}

def payment_total(pay):
    if len(pay)!=1:return None,('多币种，见实际付款金额' if pay else '未留存')
    currency,g=next(iter(pay.items()));return (None if g['missing'] else g['value']),currency

def order_row(item):
    values=[*item['prefix'],item['status']];notes=[item['note']] if item['note'] else []
    for s,m in item['metrics'].items():
        values.extend([m['state'],len(m['skus']) if m['valid'] else None,m['value'] if m['valid'] else None])
        if m['unpriced'] or m['unknown_qty']:notes.append(f"{NAMES[s]}缺价{m['unpriced']}款、数量未确认{m['unknown_qty']}款")
    for n,p in zip(PAIR_NAMES,item['pairs']):
        values.extend([p['value'] if p['valid'] and not p['unknown'] else None,len(p['skus']) if p['valid'] else None])
        if p['unknown']:notes.append(n+'资料不全，差额未汇总')
    return [*values,*payment_total(item['payment']),'；'.join(notes)]

def store_rows(items,split=False):
    groups=OrderedDict()
    for item in items:
        h=item['head'];groups.setdefault((h['store'],h['warehouse'] if split else ''),[]).append(item)
    rows=[]
    for (store,_),group in groups.items():
        names=list(dict.fromkeys(i['prefix'][2] for i in group));warehouses=list(dict.fromkeys(i['head']['warehouse'] for i in group));days=[i['head']['day'] for i in group]
        row=[store,' / '.join(names),' / '.join(warehouses),min(days),max(days),len(group)];notes=[];pay={}
        for s in STAGES:
            effective=[i['metrics'][s] for i in group if i['metrics'][s]['valid']];skus=set().union(*(m['skus'] for m in effective))
            row.extend([len(effective),len(skus) if effective else None,sum((m['value'] for m in effective),Decimal(0)) if effective else None])
            excluded=len(group)-len(effective);unknown=sum(m['unknown_qty']+m['unpriced'] for m in effective)
            if excluded:notes.append(f'{NAMES[s]}未纳入{excluded}单（未导入/待核/无需，见订单表）')
            if unknown:notes.append(f'{NAMES[s]}缺价或数量未确认{unknown}项，货值只含已计价部分')
        for index,n in enumerate(PAIR_NAMES):
            valid=[i['pairs'][index] for i in group if i['pairs'][index]['valid'] and not i['pairs'][index]['unknown']]
            row.extend([len(valid),sum((p['value'] for p in valid),Decimal(0)) if valid else None,len(set().union(*(p['skus'] for p in valid))) if valid else None])
        for item in group:
            if item['metrics']['payment']['valid']:
                for currency,g in item['payment'].items():
                    total=pay.setdefault(currency,{'value':Decimal(0),'missing':0});total['value']+=g['value'];total['missing']+=g['missing']
        rows.append([*row,*payment_total(pay),'；'.join(notes)])
    return rows

def collect(path,heads,progress=lambda _:None,cancel=None):
    if not heads:raise ValueError('请先选择订单')
    path=Path(path).resolve();items=[];seen={};cache=OrderedDict()
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('BEGIN');repo=repository(path)
        for pos,h in enumerate(heads):
            checkpoint(cancel);key=(h['oid'],h['warehouse'])
            if key in seen:
                if seen[key]!=lc.key(h):raise ValueError('同一订单不能合计多个签发修订，请刷新列表')
                continue
            seen[key]=lc.key(h);purchases.validate_head(db,h)
            if db.execute('SELECT current FROM normal_orders WHERE oid=?',(h['oid'],)).fetchone()[0]!=h['revision']:raise ValueError('订单签发已更新，请刷新后导出')
            k=(h['oid'],h['revision'])
            if k not in cache:cache[k]=repo.get(h['day'],h['store'],h['revision'],view='export')
            cache.move_to_end(k)
            if len(cache)>2:cache.popitem(last=False)
            order=cache[k]
            if not order or any(order.get(key)!=h.get(key) for key in ('oid','revision','digest','day','store')):raise ValueError('所选订单与签发快照不一致')
            view=lc.snapshot(path,h,cancel=cancel,db=db,order=order);items.append(facts(view,order,cancel));progress(RowProgress('读取订单生命周期',pos+1,len(heads)))
    return items

def all_heads(path,cache,request,progress=lambda _:None,cancel=None):
    import daily_batch
    return daily_batch.read(path,cache,{**request,'all_matched':True},cancel,progress)['records']

def export(path,heads,target,level='order',scope='',progress=lambda _:None,cancel=None):
    from xlsx_exports import stream_book,write_table
    if level not in ('order','store'):raise ValueError('未知汇总层级')
    target=Path(target).resolve()
    if target.suffix.lower()!='.xlsx' or target.exists():raise ValueError('请选择新的.xlsx文件，保留已有文件')
    items=collect(path,heads,progress,cancel);tabs=[]
    if level=='store':tabs.extend([('门店级对比',STORE_HEADERS,store_rows(items)),('门店分仓对比',STORE_HEADERS,store_rows(items,True))])
    tabs.extend([('订单级对比',ORDER_HEADERS,[order_row(i) for i in items]),('差异商品明细',DETAIL_HEADERS,[r for i in items for r in i['details']]),('实际付款金额',PAY_HEADERS,[[*i['prefix'],i['metrics']['payment']['state'],c,g['value'],g['missing']] for i in items for c,g in i['payment'].items()])])
    notes=[['导出时间',datetime.now().astimezone().isoformat()],['范围',scope or '仅所选订单'],['订单数',len(items)],['门店数',len({i['head']['store'] for i in items})],['计价口径','所有环节货值按签发时基本售价计价；付款实际金额独立按原币种列出，不使用最新商品资料。'],['缺数据','未导入、待重新核对和无需环节不当0；已确认回传/付款/收货中的0、空白、缺行遵循原保存口径。'],['汇总口径','各环节仅汇总有效订单。货值是已计价部分；差额仅汇总两环节有效、数量/差额价格齐全的订单。按可比订单数判断覆盖范围，不直接相减全范围环节合计。'],['商品款数','订单按SKU计数；门店按编码去重。同商品跨天/跨仓货值按每张单分别相加。不同库存单位不相加为总数量。'],['追加商品','签发中没有的商品视作未签发；无签发价不反填当前价格，提示缺价并保留在差异明细。'],['差额方向','后环节减前环节；负值表示减少。差异明细包括数量变化、缺价或待核资料，数量仍为各商品库存单位。'],['实际付款','单一币种显示数值；多币种不混加，见实际付款金额。未标币种保持未标；门店实际付款合计只含有效付款单。'],['历史保护','本导出只读，不更新模型、签发、生命周期、生成单或库存。当前所选签发变化时拒绝旧选择。']]
    tabs.append(('统计口径',['项目','说明'],notes));fd,tmp=tempfile.mkstemp(dir=target.parent,suffix='.xlsx');os.close(fd)
    try:
        with stream_book() as book:
            for title,headers,rows in tabs:
                # Decimal is converted only at Excel presentation, never in stored facts.
                converted=([float(v) if isinstance(v,Decimal) else v for v in r] for r in rows)
                write_table(book,title,headers,converted,style='single',cancel=cancel,progress=progress,row_count=len(rows))
            progress('压缩保存对比报表，完成后响应取消…');book.save(tmp)
        checkpoint(cancel);os.link(tmp,target)
    finally:Path(tmp).unlink(missing_ok=True)
    return {'path':str(target),'orders':len(items),'stores':len({i['head']['store'] for i in items}),'sheets':[t[0] for t in tabs]}
