"""37-column business export projection; frozen facts and exclusions are unchanged."""
from collections import OrderedDict
from decimal import Decimal
import order_analytics as service
from funnel_monitor_export import Sheet,NUMBER,INTEGER,MONEY,PERCENT
from runtime_support import checkpoint

IDENTITY=['门店编码','门店名称','供货仓','补货单号','业务开始时间','业务结束时间','必采分组']
METRICS=('商品编码数量','商品数','商品货值')
PAIRS=(*service.PAIRS,('model','receipt'))
HEADERS=IDENTITY+[service.NAMES[s]+m for s in service.STAGES for m in METRICS]+[
    ('整体：' if i==4 else '')+service.NAMES[b]+'/'+service.NAMES[a]+m+'比值'
    for i,(a,b) in enumerate(PAIRS) for m in METRICS]

def metrics(rows,stage):
    known=[r for r in rows if r['valid'][stage]]
    if not known:return (None,None,None)
    values=[service.amount(r['quantities'][stage],r['price']) for r in known]
    priced=[v for v in values if v is not None]
    return (len({r['sku'] for r in known if r['quantities'][stage]>0}),
            sum((r['quantities'][stage] for r in known),Decimal(0)),
            sum(priced,Decimal(0)) if priced else None)

def ratios(rows,a,b):
    common=[r for r in rows if r['valid'][a] and r['valid'][b]]
    before,after=metrics(common,a),metrics(common,b)
    return tuple(y/x if x is not None and y is not None and x>0 else None for x,y in zip(before,after))

def grouped(rows,level):
    groups=OrderedDict()
    for r in rows:
        key=(r['store'],r['warehouse'],r['order']) if level=='order' else (r['store'],r['warehouse']) if level=='warehouse' else (r['store'],)
        groups.setdefault(key,[]).append(r)
    return groups.values()

def records(rows,level,cancel=None):
    for rs in grouped(rows,level):
        first=rs[0];days=[r['day'] for r in rs]
        identity=[first['store'],first['store_name'],' / '.join(dict.fromkeys(r['warehouse'] for r in rs)),
                  first['order'] if level=='order' else '',min(days),max(days)]
        for group in ('全部',*service.GROUPS):
            checkpoint(cancel)
            chosen=rs if group=='全部' else [r for r in rs if r['group']==group]
            if not chosen:continue
            eligible=[r for r in chosen if r['scope']=='纳入']
            yield identity+[group]+[v for s in service.STAGES for v in metrics(eligible,s)]+[
                v for a,b in PAIRS for v in ratios(eligible,a,b)]

def write(book,name,rows,level,progress=lambda _:None,cancel=None):
    sheet=Sheet(book,name,[24,34,22,44,18,18,16]+[24]*30,cancel)
    sheet.ws.freeze_panes='H2';sheet.ws.print_title_rows='1:1'
    notes={3:'订单表填写补货单号；门店及分仓汇总涉及多单，此列留空。',4:'本行包含的签发业务日期最小值，不是首次操作的时分秒。',
           5:'本行包含的签发业务日期最大值，不代表收货完成时间。',6:'全部为合计口径；不能再与必采、非必采、属性待核相加。依据签发时清单。'}
    for i in range(5):
        notes[7+3*i]='本阶段有效数量大于0的去重商品编码款数；跨订单相同编码只计一次。未导入留空。'
        notes[8+3*i]='各商品库存单位数量相加；包、瓶、个等混合，仅作执行数量统计，不代表同质物量。未导入留空。'
        notes[9+3*i]='各商品数量×签发时单价的已知货值合计；正数量缺价不猜0，数量明确为0的货值为0，无已知货值留空。不是付款单实际金额。'
    for i in range(22,37):notes[i]='后阶段÷前阶段；只用两个阶段均有效的相同商品集合，货值还要求签发价已知。缺资料或分母0留空，允许超过100%；并非不同覆盖范围总数直接相除。'
    sheet.row(HEADERS,'header',height=72,comments=notes)
    formats={7+3*i+j:(INTEGER,NUMBER,MONEY)[j] for i in range(5) for j in range(3)}
    formats.update({i:PERCENT for i in range(22,37)})
    for values in records(rows,level,cancel):sheet.row(values,height=32,formats=formats)
    sheet.ws.auto_filter.ref=f'A1:AK{sheet.count}'
    progress(f'已生成：{name}（{sheet.count-1}行，37列）')
    return name

def write_summary(book,rows,level,progress=lambda _:None,cancel=None):
    tabs=[('门店经营对比','store'),('门店分仓对比','warehouse')] if level=='store' else []
    tabs.append(('订单五段漏斗','order'))
    return [write(book,name,rows,kind,progress,cancel) for name,kind in tabs]
