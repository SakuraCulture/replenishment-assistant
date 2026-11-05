"""Native and AI exports share these exact columns, rows and styles."""
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import hashlib
import os
import tempfile

import order_analytics as service
from runtime_support import checkpoint
from xlsx_exports import stream_book, write_table

IDENTITY=['业务日期','门店编码','门店名称','供货仓','补货单号']
DETAIL_HEADERS=IDENTITY+['商品编码','商品名称','库存单位','分类','必采属性','签发时单价']+[service.NAMES[s]+'数量' for s in service.STAGES]+[service.NAMES[s]+'资料状态' for s in service.STAGES]+['付款实际行金额','付款币种','付款依据','预算说明','人工调整原因','资料提示','源行号']
SUMMARY_HEADERS=['门店编码','门店名称','供货仓','补货单号','业务开始日','业务结束日','必采分组','纳入条目','排除条目','待核条目']+[c for s in service.STAGES for c in (service.NAMES[s]+'已计价货值',service.NAMES[s]+'款数',service.NAMES[s]+'有效订单数',service.NAMES[s]+'缺价条目')]+[c for a,b in service.PAIRS for c in (service.NAMES[b]+'−'+service.NAMES[a]+'增量货值',service.NAMES[b]+'−'+service.NAMES[a]+'减量货值',service.NAMES[b]+'−'+service.NAMES[a]+'净差',service.NAMES[b]+'/'+service.NAMES[a]+'保留率',service.NAMES[b]+'可比条目')]


def summary_rows(items):
    for item in items:
        for group,m in [('全部',item['metrics']),*item['groups'].items()]:
            row=[item['store'],item['name'],item['warehouse'],item['order'],min(item['days']),max(item['days']),group,m['included'],m['excluded'],m['pending']]
            for s in service.STAGES:
                v=m['stages'][s];row.extend([v['value'],v['skus'],v['orders'],v['unpriced']])
            for p in m['pairs']:row.extend([p['increase'],p['decrease'],p['delta'],p['ratio'],p['rows']])
            yield row


def detail_rows(rows):
    for r in rows:
        yield [r['day'],r['store'],r['store_name'],r['warehouse'],r['order'],r['sku'],r['name'],r['unit'],r['category'],r['group'],r['price'],
               *[r['quantities'][s] if r['valid'][s] else None for s in service.STAGES],
               *[r['states'][s] if r['valid'][s] else r['states'][s]+' / 数量不可比' for s in service.STAGES],
               r['payment_amount'] if r['valid']['payment'] else None,r['payment_currency'],r['payment_source'],
               r['decision'].get('budget'),r['decision'].get('manual'),'；'.join(r['reasons']+[v for v in r['notes'].values() if v]),r['source_row']]


def export(path,report,target,level='store',orders=None,progress=lambda _:None,cancel=None):
    if level not in ('store','order','detail'):raise ValueError('未知报表类型')
    target=Path(target).resolve()
    if target.suffix.lower()!='.xlsx' or target.exists():raise ValueError('请使用新的.xlsx文件，保留已导出分析快照')
    checkpoint(cancel);service.validate_snapshot(path,report,progress,cancel)
    selected=set(orders or [])
    if selected-set(h['number'] for h in report['heads']):raise ValueError('勾选订单不在当前查询范围')
    rows=[r for r in report['rows'] if not selected or r['order'] in selected]
    included=[r for r in rows if r['scope']=='纳入'];pending=[r for r in rows if r['scope']=='待核']
    tabs=[]
    if level=='store':
        tabs.extend([('门店经营对比',SUMMARY_HEADERS,list(summary_rows(service.summaries(rows,'store')))),
                     ('门店分仓对比',SUMMARY_HEADERS,list(summary_rows(service.summaries(rows,'warehouse'))))])
    if level!='detail':tabs.append(('订单五段漏斗',SUMMARY_HEADERS,list(summary_rows(service.summaries(rows,'order')))))
    tabs.extend([('全量商品五段',DETAIL_HEADERS,list(detail_rows(included))),('资料待核',DETAIL_HEADERS,list(detail_rows(pending)))])
    tabs.append(('原单实际付款', ['补货单号','门店编码','仓','币种','原单已知行金额合计','缺金额行数','资料状态','当前是否有效','范围说明'],
                 [[p['order'],p['store'],p['warehouse'],p['currency'],p['amount'],p['missing'],p['state'],'是' if p['valid'] else '否','原付款单全部商品，未应用漏斗商品过滤；缺行金额未猜分'] for p in report.get('payments',[]) if not selected or p['order'] in selected]))
    from history_diagnostics import HEADERS,review,table
    tabs.append(('多轮证据复盘',HEADERS,table(review({**report,'rows':rows},report['query'].get('rounds',8)))))
    notes=[['分析口径版本',service.VERSION],['查询快照',report['snapshot_id']],['数据截至',report['as_of']],
           ['业务开始日',report['query']['from']],['业务结束日',report['query']['to']],
           ['导出范围','勾选订单：'+'、'.join(orders) if orders else '当前成功查询的全部分页'],
           ['门店范围','、'.join(report['query']['stores']) or '全部'],['仓范围',report['query'].get('warehouse') or '全部'],
           ['必采属性',report['query'].get('group') or '必采、非必采和属性待核'],
           ['关键词',report['query'].get('search','')],['分类',report['query'].get('category','')],
           ['排除条目',sum(r['scope']=='排除' for r in rows)],['排除口径','门店禁采/拒收、对应仓禁用、虚拟分类标杆淘汰全系列；其他准入限制保持。五段统一排除。'],
           ['价格','五段货值统一按签发时基本售价。实际付款行金额、币种独立保留，缺价不填0。'],
           ['覆盖','各段已知值不直接相减；相邻变化和比率仅使用同一有效已计价商品集合。未导入、待重新核对不按0。'],
           ['必采','按签发时必卖清单；无可核实依据为属性待核，不使用当前清单补写历史。'],
           ['模型建议','当时自动建议，含启用的一箱例外，门店总预算和人工安排前；没有改动计算规则。'],
           ['数量','按商品库存单位。不同SKU单位不能混成一个总数量。'],
           ['历史保护','只读分析，不改写原签发、回传、付款、实收或库存。'],
           ['生成时间',datetime.now().astimezone().isoformat()]]
    tabs.append(('统计口径',['项目','说明'],notes))
    fd,tmp=tempfile.mkstemp(dir=target.parent,prefix='.analytics-',suffix='.xlsx');os.close(fd)
    try:
        with stream_book() as book:
            for name,headers,values in tabs:
                checkpoint(cancel)
                write_table(book,name,headers,([float(v) if isinstance(v,Decimal) else v for v in row] for row in values),
                    style='single',cancel=cancel,progress=progress,row_count=len(values))
            progress('压缩保存报表，完成后响应取消…');book.save(tmp)
        checkpoint(cancel)
        # A confirmed/deleted order while writing must not publish an obsolete report.
        service.validate_snapshot(path,report,progress,cancel)
        checkpoint(cancel);os.link(tmp,target)
    finally:Path(tmp).unlink(missing_ok=True)
    return {'path':str(target),'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
            'snapshot_id':report['snapshot_id'],'rows':len(included),'pending':len(pending),'sheets':[t[0] for t in tabs]}
