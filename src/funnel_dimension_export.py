"""Flat daily business views of the same frozen five-stage facts."""
from collections import OrderedDict
import order_analytics as service
from funnel_compact_export import metrics,ratios,PAIRS,METRICS
from funnel_monitor_export import Sheet,INTEGER,NUMBER,MONEY,PERCENT
from analytics_identifiers import HEADERS as ID_HEADERS,cells
from runtime_support import checkpoint

SPECS=OrderedDict([
    ('日期汇总',('day',)),
    ('日期-必采汇总',('day','group')),
    ('日期-店-仓',('day','store','warehouse')),
    ('店-仓-日期-必采',('store','warehouse','day','group')),
    ('日期-店-SKU-仓',('day','store','sku','warehouse')),
    ('店-类目-日期-必采-仓',('store','product_category','day','group','warehouse')),
    ('店-供应商-日期-必采-仓',('store','upstream_supplier','day','group','warehouse')),
])
EXCLUDED='已剔除SKU明细'
NAMES=[*list(SPECS)[:5],EXCLUDED,*list(SPECS)[5:]]
FIELDS={'day':['业务日期'],'store':['门店编码','门店名称'],'warehouse':['供货仓'],'group':['必采属性'],
        'sku':['商品编码','商品名称'],'product_category':['商品类目'],'upstream_supplier':['大仓上游供应商']}
META=['库存单位','涉及签发仓单数','纳入条目数','剔除条目数','待核条目数']
OMITTED_HEADERS=frozenset(('供应商资料状态','类目资料状态','五段资料覆盖','维度资料依据'))
METRIC_HEADERS=[service.NAMES[s]+m for s in service.STAGES for m in METRICS]+[
    ('整体：' if i==4 else '')+service.NAMES[b]+'/'+service.NAMES[a]+m+'比值' for i,(a,b) in enumerate(PAIRS) for m in METRICS]
TAIL=[service.NAMES[s]+'新增可售天数' for s in service.STAGES]+[
    '天数依据业务日','提示']+ID_HEADERS
EXCLUDED_HEADERS=['业务日期','门店编码','门店名称','供货仓','商品编码','商品名称','必采属性','大仓上游供应商','商品类目','计算分类组','库存单位','纳入状态']+[
    service.NAMES[s]+'原数量' for s in service.STAGES]+['剔除原因','维度资料提示']+ID_HEADERS

def joined(values):
    text='；'.join(dict.fromkeys(str(v) for v in values if v is not None and str(v)))
    return text if len(text)<=30000 else '内容超过单元格容量；逐商品依据见“日期-店-SKU-仓”或原商品明细，完整单号见“单号关联明细”'

def headers(kind):
    if kind==EXCLUDED:return EXCLUDED_HEADERS
    dims=SPECS[kind]
    return [h for d in dims for h in FIELDS[d]]+(['必采属性','大仓上游供应商','商品类目'] if 'sku' in dims else [])+META+METRIC_HEADERS+TAIL

def dimension(rs,key):
    r=rs[0]
    if key=='store':return [r['store'],joined(x['store_name'] for x in rs)]
    if key=='sku':return [r['sku'],joined(x.get('name') for x in rs)]
    if key=='upstream_supplier':return [r.get(key) or '供应商待核']
    if key=='product_category':return [r.get(key) or '类目待核']
    return [r[key]]

def grouped(rows,kind,cancel=None):
    groups=OrderedDict();dims=SPECS[kind]
    for r in rows:
        checkpoint(cancel);key=tuple(r.get(k) or None for k in dims)
        groups.setdefault(key,[]).append(r)
    for key in sorted(groups,key=lambda x:tuple('' if v is None else str(v) for v in x)):yield groups[key]

def records(rows,kind,cancel=None):
    from coverage_metrics import summarize
    if kind==EXCLUDED:
        for r in rows:
            checkpoint(cancel)
            if r['scope']!='排除':continue
            yield [r['day'],r['store'],r['store_name'],r['warehouse'],r['sku'],r.get('name'),r['group'],r.get('upstream_supplier') or '供应商待核',
                r.get('product_category') or '类目待核',r.get('category'),r.get('unit'),r['scope'],
                *[r['quantities'][s] if r['valid'][s] else None for s in service.STAGES],
                joined(r.get('reasons',[])),joined(r.get('dimension_notes',[])),*cells([r])]
        return
    for rs in grouped(rows,kind,cancel):
        checkpoint(cancel);eligible=[x for x in rs if x['scope']=='纳入'];coverage=summarize(eligible,cancel=cancel)
        identity=[v for k in SPECS[kind] for v in dimension(rs,k)]
        if 'sku' in SPECS[kind]:identity += [joined(x['group'] for x in rs),joined(x.get('upstream_supplier') or '供应商待核' for x in rs),joined(x.get('product_category') or '类目待核' for x in rs)]
        identity += [joined(x.get('unit') for x in rs),len({x['order'] for x in rs}),len(eligible),sum(x['scope']=='排除' for x in rs),sum(x['scope']=='待核' for x in rs)]
        notes=joined(v for x in rs for v in [*x.get('dimension_notes',[]),*x.get('reasons',[]),*x.get('notes',{}).values()])
        coverage_notes=joined(v for reasons in coverage['stage_reasons'].values() for v in reasons)
        yield identity+[v for s in service.STAGES for v in metrics(eligible,s)]+[v for a,b in PAIRS for v in ratios(eligible,a,b)]+[
            coverage['stage_days'][s] for s in service.STAGES]+[coverage['basis_day'],joined([notes,coverage_notes])]+cells(rs)

def write_view(book,rows,kind,progress=lambda _:None,cancel=None):
    from openpyxl.utils import get_column_letter
    heads=headers(kind)
    widths=[36 if h in ('门店名称','商品名称','大仓上游供应商','商品类目') else 52 if h in ('提示','剔除原因','维度资料依据') else 24 for h in heads]
    sheet=Sheet(book,kind,widths,cancel);sheet.ws.freeze_panes='B2';sheet.ws.print_title_rows='1:1'
    formats={};comments={}
    for i,h in enumerate(heads):
        if h.endswith('比值'):formats[i]=PERCENT;comments[i]='后阶段÷前阶段，仅使用两阶段共同有效商品；分母0留空，不平均其他行的比率。'
        elif h.endswith('商品货值'):formats[i]=MONEY;comments[i]='签发时单价×数量的已知货值；缺价不补0，不等于付款原表实际金额。'
        elif h.endswith('商品编码数量'):formats[i]=INTEGER;comments[i]='本组本环节有效且正数量的去重SKU数；跨组款数不能相加冒充总体。'
        elif h.endswith('商品数'):formats[i]=NUMBER;comments[i]='库存单位数量的执行统计；单位混合时不代表同质物量。未取得留空，明确0保留0。'
        elif h.endswith('新增可售天数'):formats[i]='0.0';comments[i]='本组冻结预测、签发价加权；不是当前库存，缺失或冲突不填0。'
        elif h in META[1:]:formats[i]=INTEGER
        elif h in ID_HEADERS:comments[i]='按签发仓单逐行对应；缺号如实标明，完整关系见“单号关联明细”。'
        elif h=='大仓上游供应商':comments[i]='签发时本仓冻结资料中的上游供应商名称；不是中心仓供应商编码，不读取当前资料回填历史。'
        elif h=='商品类目':comments[i]='签发时本仓原商品类目，与快销/日百计算分类组分开；来源缺失或冲突明确待核。'
    if kind==EXCLUDED:comments[heads.index('纳入状态')]='只列业务排除的SKU；待核行在“日期-店-SKU-仓”的待核条目数及提示中保留。原数量不计入其他汇总指标。'
    else:comments[heads.index('待核条目数')]='未纳入指标的待核行保留在日/店/SKU汇总及提示中，不计为0。'
    sheet.row(heads,'header',height=72,comments=comments)
    for values in records(rows,kind,cancel):
        height=min(160,max(36,18*max(str(v).count('\n')+1 for v in values[-3:])))
        sheet.row(values,height=height,formats=formats)
    sheet.ws.auto_filter.ref=f'A1:{get_column_letter(len(heads))}{sheet.count}'
    progress(f'已生成：{kind}（{sheet.count-1}行）');return kind

def write(book,rows,progress=lambda _:None,cancel=None):
    return [write_view(book,rows,kind,progress,cancel) for kind in NAMES]
