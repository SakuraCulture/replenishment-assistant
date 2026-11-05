"""Evidence-only multi-round review. No invented daily sales or receipt dates."""
from collections import OrderedDict
from decimal import Decimal
import order_analytics as analytics

HEADERS=['门店编码','门店名称','商品编码','商品名称','库存单位','轮次日期','供货仓','必采属性','月销','可用库存','在途库存','当轮签发量','当轮实收量','观察到缺货','上轮业务日期','链路证据','参数判断','证据限制']


def review(report,rounds=8):
    if type(rounds) is not int or not 1<=rounds<=30:raise ValueError('复盘轮数须在1至30之间')
    by_store=OrderedDict()
    for row in report['rows']:by_store.setdefault(row['store'],[]).append(row)
    timelines=[];stores=[]
    for code,rows in by_store.items():
        days=sorted({r['day'] for r in rows},reverse=True)[:rounds]
        chosen=[r for r in rows if r['day'] in days and r['scope']=='纳入']
        skus=OrderedDict()
        for row in chosen:skus.setdefault(row['sku'],[]).append(row)
        repeated=0;mandatory_repeated=0;stockout_observations=0
        for sku,observations in skus.items():
            observations=sorted(observations,key=lambda r:r['day']);previous=None;seen_days=set();missing_days=set()
            for row in observations:
                o=row['observations'];k,j,l=(analytics.num(o.get(f)) for f in ('K','J','L'))
                stockout=None if k is None or j is None else k<=0 and j>0
                if stockout:missing_days.add(row['day']);stockout_observations+=row['day'] not in seen_days
                seen_days.add(row['day']);evidence=[];limitations=['月销为滚动窗口，不能相减冒充期间销量；实收数量不证明准时到货。']
                if previous:
                    if previous['day']==row['day']:
                        limitations.append('同日跨仓存在同商品记录，不重复累计门店库存，也不当作两轮。')
                    elif not row['unit'] or row['unit']!=previous['unit']:
                        limitations.append('库存单位未明确或跨轮不一致，停止数量归因。')
                    else:
                        for a,b in analytics.PAIRS:
                            if previous['valid'][a] and previous['valid'][b]:
                                before,after=previous['quantities'][a],previous['quantities'][b]
                                if after<before:
                                    label=analytics.NAMES[a]+'→'+analytics.NAMES[b]+f'减少 {before-after:g}'
                                    if a=='model':
                                        detail=previous['decision'].get('manual') or previous['decision'].get('budget')
                                        label+='（'+str(detail)+'）' if detail else '（原因未留存）'
                                    evidence.append(label)
                                elif after>before:evidence.append(analytics.NAMES[a]+'→'+analytics.NAMES[b]+f'增加 {after-before:g}')
                            else:limitations.append(analytics.NAMES[a]+'→'+analytics.NAMES[b]+'资料不完整，不能确认落实差额。')
                else:limitations.append('当前范围内没有更早有效轮次，不能归因上一轮。')
                if l is not None and l>0 and stockout:limitations.append('当前有在途，需核对到店时间，不能等同于已可销售库存。')
                hypothesis='没有足够证据建议调参'
                if stockout and previous and previous['day']!=row['day'] and row['unit'] and row['unit']==previous['unit']:
                    drops=any(previous['valid'][a] and previous['valid'][b] and previous['quantities'][b]<previous['quantities'][a] for a,b in analytics.PAIRS)
                    complete=all(previous['valid'].values())
                    hypothesis='先核对执行减量与履约，不能直接归因参数' if drops else '需补齐履约证据后判断' if not complete else '可列为预测或保护期疑点；先核实销量变化、到货时间及其他出入库'
                timelines.append({'store':code,'name':row['store_name'],'sku':sku,'product':row['name'],'unit':row['unit'],
                    'day':row['day'],'warehouse':row['warehouse'],'group':row['group'],'monthly':j,'stock':k,'inbound':l,
                    'signed':row['quantities']['signed'] if row['valid']['signed'] else None,
                    'receipt':row['quantities']['receipt'] if row['valid']['receipt'] else None,
                    'observed_stockout':stockout,'previous_day':previous['day'] if previous else None,
                    'evidence':evidence,'hypothesis':hypothesis,'limitations':list(dict.fromkeys(limitations))})
                previous=row
            if len(missing_days)>=2:
                repeated+=1;mandatory_repeated+=any(r['group']=='必采' for r in observations)
        stores.append({'store':code,'name':rows[0]['store_name'],'rounds':len(days),'days':days,
            'repeated_stockout_skus':repeated,'mandatory_repeated_skus':mandatory_repeated,
            'stockout_observations':stockout_observations,'pending_rows':sum(r['scope']=='待核' for r in rows if r['day'] in days),
            'note':'轮次按不同签发业务日计数；缺货为销售有记录且当前可用≤0的离散观察，不是期间缺货率。'})
    return {'snapshot_id':report['snapshot_id'],'as_of':report['as_of'],'query':report['query'],
            'requested_rounds':rounds,'stores':stores,'items':timelines,
            'boundary':'执行差异是事实；参数归因是待验证假设。未做连续库存回测，不自动改参数。'}


def table(review):
    return [[r['store'],r['name'],r['sku'],r['product'],r['unit'],r['day'],r['warehouse'],r['group'],
             r['monthly'],r['stock'],r['inbound'],r['signed'],r['receipt'],
             '待核' if r['observed_stockout'] is None else '是' if r['observed_stockout'] else '否',
             r['previous_day'],'；'.join(r['evidence']),r['hypothesis'],'；'.join(r['limitations'])] for r in review['items']]
