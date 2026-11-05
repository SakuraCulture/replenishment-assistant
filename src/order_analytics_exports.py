"""Native and AI exports share these exact columns, rows and styles."""
from decimal import Decimal
from pathlib import Path
import hashlib
import os
import tempfile

import order_analytics as service
from runtime_support import checkpoint
from xlsx_exports import stream_book, write_table
from funnel_compact_export import write_summary, HEADERS as SUMMARY_HEADERS

IDENTITY=['业务日期','门店编码','门店名称','供货仓','补货单号']
DETAIL_HEADERS=IDENTITY+['商品编码','商品名称','库存单位','分类','必采属性','签发时单价']+[service.NAMES[s]+'数量' for s in service.STAGES]+[service.NAMES[s]+'资料状态' for s in service.STAGES]+['付款实际行金额','付款币种','付款依据','预算说明','人工调整原因','资料提示','源行号']
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
    if level=='detail':
        tabs=[('全量商品五段',DETAIL_HEADERS,list(detail_rows(included))),
              ('资料待核',DETAIL_HEADERS,list(detail_rows(pending)))]
    fd,tmp=tempfile.mkstemp(dir=target.parent,prefix='.analytics-',suffix='.xlsx');os.close(fd)
    try:
        with stream_book() as book:
            monitor = write_summary(book,rows,level,progress,cancel) if level!='detail' else []
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
            'snapshot_id':report['snapshot_id'],'rows':len(included),'pending':len(pending),'sheets':monitor+[t[0] for t in tabs]}
