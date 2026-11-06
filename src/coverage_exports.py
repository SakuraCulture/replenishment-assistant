"""Excel and native-detail presentation for shared coverage facts."""
from coverage_metrics import STAGES, NAMES, VERSION, DEFINITIONS, item, summarize
from funnel_monitor_export import Sheet
from runtime_support import checkpoint

DAYS = '0.0'
STAGE_HEADERS = [NAMES[s]+'新增可售天数' for s in STAGES]
DETAIL_HEADERS = ['业务日期','门店编码','门店名称','供货仓','补货单号','商品编码','商品名称','库存单位','必采分组',
                  '预测日均','可用库存','在途库存','现货可售天数','含在途覆盖天数','签发后预计覆盖天数','当时保护期天数',
                  *STAGE_HEADERS,'签发至实收减少天数','依据及风险提示']


def explanation(d):
    reasons = list(dict.fromkeys(d['base_reasons']+d['position_reasons']+[d['price_reason']] +
                 [v for s in STAGES for v in d['stage_reasons'][s]] + d['risks']))
    return '；'.join(x for x in reasons if x) or '按制单时预测；实收新增天数不代表当前库存'


def detail_rows(rows, cancel=None):
    for row in rows:
        checkpoint(cancel);d=item(row)
        yield [row['day'],row['store'],row['store_name'],row['warehouse'],row['order'],row['sku'],row['name'],row['unit'],row['group'],
               d['forecast_daily'],d['available'],d['inbound'],d['available_days'],d['position_days'],d['signed_position_days'],d['protection_days'],
               *d['stage_days'].values(),d['signed_to_receipt_loss_days'],explanation(d)]


def summary_note(metric, stage):
    reasons = metric['stage_reasons'][stage]
    return (f"依据业务日：{metric['basis_day'] or '无'}；本次商品按货值加权；有效{metric['eligible_rows']}/{metric['round_rows']}条，"
            f"{metric['orders']}/{metric['round_orders']}单。" + '；'.join(reasons) +
            ('；跨多轮只取最新日，不累计历史库存。' if metric['input_rows']!=metric['round_rows'] else '') +
            '；'.join(metric['excluded_reasons']))


def append_sheets(book, rows, detail=False, rounds=False, cancel=None, progress=lambda _:None):
    names=[]
    if detail:
        sh=Sheet(book,'可售天数商品',[18,24,30,16,42,24,30,12,16]+[21]*13+[65],cancel)
        sh.ws.freeze_panes='J2';sh.row(DETAIL_HEADERS,'header',height=55)
        for values in detail_rows(rows,cancel):sh.row(values,height=36,formats={i:DAYS for i in range(12,22)})
        names.append(sh.ws.title);progress('已生成可售天数商品明细')
    if rounds:
        from collections import OrderedDict
        grouped=OrderedDict()
        for r in rows:grouped.setdefault((r['store'],r['day']),[]).append(r)
        sh=Sheet(book,'门店逐轮天数',[24,30,18,22,16]+[22]*5+[60],cancel)
        sh.row(['门店编码','门店名称','业务日期','供货仓','必采分组',*STAGE_HEADERS,'有效范围与待核说明'],'header',height=55)
        for rs in grouped.values():
            warehouses=list(dict.fromkeys(r['warehouse'] for r in rs))
            for warehouse in ['全部仓',*warehouses]:
                scoped=rs if warehouse=='全部仓' else [r for r in rs if r['warehouse']==warehouse]
                for group in ('全部','必采','非必采','属性待核'):
                    chosen=scoped if group=='全部' else [r for r in scoped if r['group']==group]
                    if not chosen:continue
                    metric=summarize(chosen,cancel=cancel)
                    sh.row([rs[0]['store'],rs[0]['store_name'],rs[0]['day'],warehouse,group,*metric['stage_days'].values(),
                            '；'.join(dict.fromkeys(summary_note(metric,s) for s in STAGES))],height=36,formats={i:DAYS for i in range(5,10)})
        names.append(sh.ws.title);progress('已生成门店逐轮天数')
    sh=Sheet(book,'可售天数口径',[32,105],cancel);sh.row(['项目','说明'],'header')
    sh.row(['指标版本',VERSION]);sh.row(['依据业务日',' / '.join(dict.fromkeys(r['day'] for r in rows))])
    labels={'forecast':'预测依据','increment':'五段新增天数','stock':'库存覆盖','weighted':'门店汇总','missing':'缺失与异常','cohort':'可比范围','rounds':'多轮查询','warehouse':'跨仓合计'}
    for key,value in DEFINITIONS.items():sh.row([labels[key],value],height=44)
    metric=summarize(rows,cancel=cancel)
    sh.row(['最新轮次有效范围',f"{metric['basis_day']}；有效{metric['eligible_rows']}/{metric['round_rows']}条；{metric['orders']}/{metric['round_orders']}单"],height=44)
    for reason,count in metric['excluded_reasons'].items():sh.row([reason,count])
    for conflict in metric['conflicts']:sh.row(['跨仓依据待核',f"{conflict['store']} / {conflict['sku']}：{conflict['reason']}"])
    names.append(sh.ws.title)
    return names
