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
from analytics_identifiers import HEADERS as ID_HEADERS, LINK_HEADERS, cells,link_rows
from funnel_dimension_export import OMITTED_HEADERS

IDENTITY=['业务日期','门店编码','门店名称','供货仓','补货单号']
DIMENSION_HEADERS=['大仓上游供应商','商品类目','供应商资料状态','类目资料状态','维度资料提示','维度资料依据']
DETAIL_HEADERS=IDENTITY+['商品编码','商品名称','库存单位','计算分类组','必采属性','签发时单价']+[service.NAMES[s]+'数量' for s in service.STAGES]+[service.NAMES[s]+'资料状态' for s in service.STAGES]+['付款实际行金额','付款币种','付款依据','预算说明','人工调整原因','资料提示','源行号']+DIMENSION_HEADERS+ID_HEADERS
# Keep the existing read-only on-screen detail contract. Only Excel omits the
# four fields requested by the business; quantities and identities stay aligned.
EXPORT_DETAIL_INDEXES=tuple(i for i,h in enumerate(DETAIL_HEADERS) if h not in OMITTED_HEADERS)
EXPORT_DETAIL_HEADERS=[DETAIL_HEADERS[i] for i in EXPORT_DETAIL_INDEXES]

def export_detail_rows(rows):
    for row in detail_rows(rows):yield [row[i] for i in EXPORT_DETAIL_INDEXES]
def detail_rows(rows):
    for r in rows:
        yield [r['day'],r['store'],r['store_name'],r['warehouse'],r['order'],r['sku'],r['name'],r['unit'],r['category'],r['group'],r['price'],
               *[r['quantities'][s] if r['valid'][s] else None for s in service.STAGES],
               *[r['states'][s] if r['valid'][s] else r['states'][s]+' / 数量不可比' for s in service.STAGES],
               r['payment_amount'] if r['valid']['payment'] else None,r['payment_currency'],r['payment_source'],
               r['decision'].get('budget'),r['decision'].get('manual'),'；'.join(r['reasons']+[v for v in r['notes'].values() if v]),r['source_row'],
               r.get('upstream_supplier') or '供应商待核',r.get('product_category') or '类目待核',r.get('supplier_state','供应商待核'),r.get('category_state','类目待核'),
               '；'.join(r.get('dimension_notes',[])),r.get('dimension_sources'),*cells([r])]


def export(path,report,target,level='store',orders=None,progress=lambda _:None,cancel=None):
    if level not in ('store','order','detail','coverage_rounds','workflow','analysis','analysis_detail'):raise ValueError('未知报表类型')
    target=Path(target).resolve()
    if target.suffix.lower()!='.xlsx' or target.exists():raise ValueError('请使用新的.xlsx文件，保留已导出分析快照')
    checkpoint(cancel);service.validate_snapshot(path,report,progress,cancel)
    if report.get('linked_stockout'):
        import workflow_stockout
        workflow_stockout.validate(path,report['query'],report['linked_stockout'],cancel)
    selected=set(orders or [])
    if selected-set(h['number'] for h in report['heads']):raise ValueError('勾选订单不在当前查询范围')
    rows=[r for r in report['rows'] if not selected or r['order'] in selected]
    if level in ('analysis','analysis_detail'):
        return export_template(path,report,target,level,rows,progress,cancel)
    included=[r for r in rows if r['scope']=='纳入'];pending=[r for r in rows if r['scope']=='待核']
    tabs=[]
    if level=='detail':
        tabs=[('全量商品五段',EXPORT_DETAIL_HEADERS,list(export_detail_rows(included))),
              ('资料待核',EXPORT_DETAIL_HEADERS,list(export_detail_rows(pending)))]
    fd,tmp=tempfile.mkstemp(dir=target.parent,prefix='.analytics-',suffix='.xlsx');os.close(fd)
    try:
        with stream_book() as book:
            monitor=[]
            if level in ('store','order','workflow'):
                from workflow_exports import append
                monitor=append(book,report,rows,progress,cancel,custom=level=='workflow')
            if level=='order':monitor.extend(write_summary(book,rows,level,progress,cancel,coverage=True,references=True))
            if level in ('store','order'):
                from funnel_dimension_export import write
                monitor.extend(write(book,rows,progress,cancel))
            for name,headers,values in tabs:
                checkpoint(cancel)
                write_table(book,name,headers,([float(v) if isinstance(v,Decimal) else v for v in row] for row in values),
                    style='single',cancel=cancel,progress=progress,row_count=len(values))
            from coverage_exports import append_sheets
            extra=append_sheets(book,rows,detail=level=='detail',rounds=level in ('store','coverage_rounds'),cancel=cancel,progress=progress)
            from funnel_monitor_export import Sheet
            associations=Sheet(book,'单号关联明细',[18,26,38,18,46,36,36,22,26,44,22,28],cancel)
            associations.ws.freeze_panes='H2';associations.ws.print_title_rows='1:1'
            associations.row(LINK_HEADERS,'header',height=48,comments={4:'签发单号即原补货仓单号。按当前本机签发身份、修订和仓关联。',
                5:'优先使用本机维护的聚水潭内部号；否则使用当前付款原表的内部订单号。',
                8:'本机已留存状态，不表示本次在线查询；已作废号码仍留存。'})
            for values in link_rows(rows,cancel):associations.row(values,height=34)
            associations.ws.auto_filter.ref=f'A1:L{associations.count}';extra.append('单号关联明细')
            progress('压缩保存报表，完成后响应取消…');book.save(tmp)
        checkpoint(cancel)
        # A confirmed/deleted order while writing must not publish an obsolete report.
        service.validate_snapshot(path,report,progress,cancel)
        if report.get('linked_stockout'):workflow_stockout.validate(path,report['query'],report['linked_stockout'],cancel)
        checkpoint(cancel);os.link(tmp,target)
    finally:Path(tmp).unlink(missing_ok=True)
    return {'path':str(target),'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
            'snapshot_id':report['snapshot_id'],'rows':len(included),'pending':len(pending),'sheets':monitor+[t[0] for t in tabs]+extra,'coverage_version':report['coverage']['version']}


def export_template(path,report,target,level,rows,progress,cancel):
    """The new presentation uses the same frozen query and atomic publication."""
    from analytics_formula_cache import FormulaCache
    from analytics_template_export import append_summary,append_detail
    fd,tmp=tempfile.mkstemp(dir=target.parent,prefix='.analytics-',suffix='.xlsx');os.close(fd)
    try:
        with FormulaCache(target.parent,cancel) as cache:
            with stream_book() as book:
                book._analytics_formula_cache=cache.add
                writer=append_summary if level=='analysis' else append_detail
                sheets=writer(book,report,rows,progress,cancel)
                checkpoint(cancel);progress('保存模板报表与公式缓存…');book.save(tmp)
            cache.apply(tmp)
        service.validate_snapshot(path,report,progress,cancel)
        if report.get('linked_stockout'):
            import workflow_stockout
            workflow_stockout.validate(path,report['query'],report['linked_stockout'],cancel)
        checkpoint(cancel);os.link(tmp,target)
    finally:Path(tmp).unlink(missing_ok=True)
    with target.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
    return {'path':str(target),'sha256':digest,'snapshot_id':report['snapshot_id'],
            'rows':sum(r['scope']=='纳入' for r in rows),'pending':sum(r['scope']=='待核' for r in rows),
            'sheets':sheets,'coverage_version':report['coverage']['version'],'template':'summary' if level=='analysis' else 'detail'}
