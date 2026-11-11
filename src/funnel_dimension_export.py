"""One flat SKU/store/warehouse/historical supplier/category projection."""
from collections import OrderedDict
import order_analytics as service
from funnel_compact_export import metrics,ratios,PAIRS,METRICS
from funnel_monitor_export import Sheet,INTEGER,NUMBER,MONEY,PERCENT
from analytics_identifiers import HEADERS as ID_HEADERS,cells
from runtime_support import checkpoint

NAME='门店SKU供应商类目'
IDENTITY=['门店编码','门店名称','供货仓','商品编码','商品名称','大仓上游供应商','商品类目','计算分类组','库存单位','必采属性','纳入状态','业务开始日期','业务结束日期','涉及签发仓单数']
HEADERS=IDENTITY+[service.NAMES[s]+m for s in service.STAGES for m in METRICS]+[
    ('整体：' if i==4 else '')+service.NAMES[b]+'/'+service.NAMES[a]+m+'比值' for i,(a,b) in enumerate(PAIRS) for m in METRICS]+[
    service.NAMES[s]+'新增可售天数' for s in service.STAGES]+['天数依据业务日','供应商资料状态','类目资料状态','五段资料覆盖','维度与纳入提示','维度资料依据']+ID_HEADERS

def joined(values):return '；'.join(dict.fromkeys(str(v) for v in values if v is not None and str(v)))

def records(rows,cancel=None):
    from coverage_metrics import summarize
    groups=OrderedDict()
    for r in rows:
        checkpoint(cancel)
        key=tuple(r.get(k) for k in ('store','warehouse','sku','upstream_supplier','product_category','category','unit','group','scope'))
        groups.setdefault(key,[]).append(r)
    for rs in groups.values():
        checkpoint(cancel);r=rs[0];eligible=[x for x in rs if x['scope']=='纳入'];days=[x['day'] for x in rs]
        coverage=summarize(eligible,cancel=cancel)
        identity=[r['store'],joined(x['store_name'] for x in rs),r['warehouse'],r['sku'],joined(x['name'] for x in rs),
                  r.get('upstream_supplier') or '供应商待核',r.get('product_category') or '类目待核',r['category'],r['unit'],r['group'],r['scope'],min(days),max(days),len({x['order'] for x in rs})]
        notes=joined(v for x in rs for v in [*x.get('dimension_notes',[]),*x.get('reasons',[])])
        known=joined(service.NAMES[s]+f"有效{sum(x['valid'][s] for x in eligible)}/{len(eligible)}条" for s in service.STAGES)
        yield identity+[v for s in service.STAGES for v in metrics(eligible,s)]+[v for a,b in PAIRS for v in ratios(eligible,a,b)]+[
            coverage['stage_days'][s] for s in service.STAGES]+[coverage['basis_day'],r.get('supplier_state','供应商待核'),r.get('category_state','类目待核'),known,notes,joined(x.get('dimension_sources') for x in rs)]+cells(rs)

def write(book,rows,progress=lambda _:None,cancel=None):
    from openpyxl.utils import get_column_letter
    sheet=Sheet(book,NAME,[24,36,18,26,46,36,30,20,16,18,16,18,18,22]+[24]*(len(HEADERS)-14),cancel)
    sheet.ws.freeze_panes='F2';sheet.ws.print_title_rows='1:1'
    comments={5:'签发时所属仓归档商品原件中的供应商名称；不是补货建议中的中心仓供应商编码。同名供应商按供货仓区分，不合并别称。',
              6:'当时原商品类目；签发、冻结仓资料及原件均有值时必须一致。与快销/日百等计算分类组分开。',
              10:'沿用原五段纳入规则。供应商或类目缺失只标记维度待核，不使原本纳入的商品退出指标；排除或业务待核行仍列出，指标留空。',
              13:'同一门店+仓+SKU+供应商+类目+计算分类组+单位+必采属性+纳入状态一行；跨日期相同组合合并，签发单数去重。'}
    start=len(IDENTITY)
    formats={13:INTEGER}
    for i in range(5):
        for j,fmt in enumerate((INTEGER,NUMBER,MONEY)):formats[start+3*i+j]=fmt
        comments[start+3*i]='本环节有效且正数量的去重SKU数。不同供应商/类目组合款数不能相加冒充总体。'
        comments[start+3*i+1]='汇总纳入商品已知数量。未取得留空、明确0保留0；部分有效范围见五段资料覆盖。库存单位不同拆行。'
        comments[start+3*i+2]='按每条签发时单价计算的已知货值，不是付款原表实际金额；缺价不补0。'
    for i in range(start+15,start+30):formats[i]=PERCENT;comments[i]='后阶段÷前阶段，只使用两阶段共同有效的原商品集合；分母0留空，不平均其他行比率。'
    for i in range(start+30,start+35):formats[i]='0.0';comments[i]='固定制单预测、签发价加权，取本组合最新业务日。不能跨组求和或平均；不是当前库存。'
    for i in range(len(HEADERS)-3,len(HEADERS)):comments[i]='按签发仓单逐行对应；缺少外部号如实标明。完整关系见单号关联明细。'
    sheet.row(HEADERS,'header',height=72,comments=comments)
    for values in records(rows,cancel):sheet.row(values,height=50,formats=formats)
    sheet.ws.auto_filter.ref=f'A1:{get_column_letter(len(HEADERS))}{sheet.count}'
    progress(f'已生成：{NAME}（{sheet.count-1}行）');return NAME
