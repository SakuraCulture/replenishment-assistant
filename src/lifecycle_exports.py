"""Returned merchant sheets reuse the signed layout, never the signed quantities."""
import copy,json,os,tempfile,uuid
from contextlib import closing
from decimal import Decimal
from pathlib import Path
import openpyxl
from openpyxl.comments import Comment
from openpyxl.cell import WriteOnlyCell
from openpyxl.packaging.custom import StringProperty
import normal_exports as ne
import order_identity as identity
from history_read import repository
from runtime_support import checkpoint,Cancelled
from export_progress import RowProgress
from xlsx_exports import stream_book


def retained_versions(path, heads, stage, cancel):
    """Freeze the selected stage versions in one view without loading other stages."""
    import archive_orders
    import lifecycle_store
    import purchases
    views = []
    title = '门店回传单' if stage == 'return' else '运营签发单'
    with closing(purchases.connect(path)) as db:
        db.execute('BEGIN')
        for head in heads:
            checkpoint(cancel)
            archive_orders.require_active(db, head['oid'], head['warehouse'])
            version = lifecycle_store.latest_version(db, head, stage) if lifecycle_store.exists(db) else None
            if version is None:
                raise ValueError(head['number'] + '尚未导入' + title)
            views.append({'head': head, 'version': version})
    return views

def returned_book(path,view,folder,progress,cancel,stage='return'):
    h=view['head']
    import archive_orders
    import warehouse_execution
    repo = repository(path)
    with closing(repo.work.connect()) as db:
        db.execute('BEGIN')
        archive_orders.require_active(db, h['oid'], h['warehouse'])
        if 'version' in view:
            from lifecycle_facts import current
            version, digest = view['version']
            doc = current(db, h, stage, version)
            if not doc or doc['digest'] != digest:
                raise ValueError(h['number'] + '原环节版本已变化，请重新核对')
        else:
            doc = view['documents'][stage]
        order = warehouse_execution.get_order(repo, h, view='export', progress=progress, db=db)
    if order['digest']!=h['digest']:raise ValueError('签发修订校验失败')
    original=Path(folder)/('original-'+uuid.uuid4().hex+'.xlsx');ne.export(order,original,warehouse=h['warehouse'],progress=progress,cancel=cancel)
    b=openpyxl.load_workbook(original)
    original.unlink()
    rows={r['sku']:r for r in doc['payload']['rows']};raw_rows={r['sku']:r for r in doc['payload'].get('raw_rows',doc['payload']['rows'])};seen=set();last_rows={}
    try:
        for ws in b:
            if ws.title==identity.SHEET:continue
            ws['Q1'].comment=Comment('补货数量为本版'+('门店确认数' if stage=='return' else '运营确认数')+'；原签发与门店回传独立保留在应用中。','补货助手')
            last_rows[ws.title] = ws.max_row
            for n in range(2,last_rows[ws.title]+1):
                checkpoint(cancel);sku=str(ws.cell(n,5).value);r=rows.get(sku);seen.add(sku)
                ws.cell(n,17).value=float(Decimal(r['base_quantity'])) if r and r['base_quantity'] is not None else None
                ws.cell(n,17).comment=None
        for r in doc['payload']['rows']:
            if r['sku'] in seen:continue
            checkpoint(cancel);source_sheet=next((s['sheet'] for s in r['sources'] if s['sheet'] in ne.SHEETS),ne.SHEETS[-1]);ws=b[source_sheet]
            source=raw_rows.get(r['sku'],r);raw={k:v for k,v in zip(source.get('raw_headers',[]),source.get('raw_values',[]))};values=[raw.get(k) for k in ne.HEADERS+ne.MERCHANT_AUDIT_HEADERS]
            values[0]=order['payload']['result']['store_name'];values[1]=h['store'];values[3]=r['name'];values[4]=r['sku'];values[16]=float(Decimal(r['base_quantity']));values[18]=h['warehouse'];values[20]='门店追加'
            ws.append(values)
            last_rows[ws.title] += 1
            row = last_rows[ws.title]
            for i,c in enumerate(ws[row],1):
                c._style=copy.copy(ws.cell(2 if row>2 else 1,i)._style)
                if isinstance(c.value,str):c.data_type='s'
            ws.cell(row,16).number_format='0.00';ws.cell(row,18).number_format='@'
        b.custom_doc_props.append(StringProperty(name='门店回传版本' if stage=='return' else '运营签发版本',value=str(doc['version'])))
        b.custom_doc_props.append(StringProperty(name='补货助手生命周期环节',value=stage))
        b.properties.subject='门店回传单，数量为门店确认数' if stage=='return' else '运营签发单，数量为运营确认数'
        return b
    except Exception:b.close();raise


def copy_sheet_layout(source, target):
    for field in ('sheet_format', 'sheet_properties', 'views', 'protection', 'HeaderFooter',
                  'print_options', 'page_setup', 'page_margins', 'auto_filter'):
        setattr(target, field, copy.copy(getattr(source, field)))
    target.sheet_state = source.sheet_state
    for field in ('column_dimensions', 'row_dimensions'):
        dimensions = getattr(target, field)
        for key, value in getattr(source, field).items():
            dimensions[key] = copy.copy(value)
            dimensions[key].parent = target


def append_sheet(source, target, start, styles, cancel):
    for row in source.iter_rows(min_row=start):
        checkpoint(cancel)
        cells = []
        for original in row:
            cell = WriteOnlyCell(target, original.value)
            cell.data_type = original.data_type
            if original.has_style:
                key = tuple(original._style)
                if key not in styles:
                    # Style IDs are local to each source workbook.
                    for field in ('font', 'fill', 'border', 'alignment', 'number_format', 'protection'):
                        setattr(cell, field, copy.copy(getattr(original, field)))
                    styles[key] = cell._style
                else:
                    cell._style = styles[key]
            if original.comment is not None:
                cell.comment = copy.copy(original.comment)
            cells.append(cell)
        target.append(cells)


def merge_returns(path, group, temp, stage, numbers, progress, cancel):
    title = '门店回传单' if stage == 'return' else '运营签发单'
    file = Path(temp)/'returned.xlsx'
    identities = []
    with stream_book() as book:
        for index, view in enumerate(group):
            progress(RowProgress('整理'+title+' '+view['head']['number'], index, len(group)))
            with closing(returned_book(path, view, temp, progress, cancel, stage)) as part:
                identities.extend(identity.read(part))
                styles = {}
                if index == 0:
                    book.properties = copy.copy(part.properties)
                    book.custom_doc_props = copy.deepcopy(part.custom_doc_props)
                    for name in ('补货单号', '修订', '门店正常单号', '门店回传版本', '运营签发版本'):
                        if name in book.custom_doc_props.names:
                            del book.custom_doc_props[name]
                    book.custom_doc_props.append(StringProperty(name='签发修订索引', value=json.dumps(numbers, ensure_ascii=False)))
                for source in part:
                    if source.title == identity.SHEET:
                        continue
                    if index == 0:
                        dest = book.create_sheet(source.title)
                        copy_sheet_layout(source, dest)
                        dest.oddHeader.center.text = view['head']['warehouse']+' 多订单'+title
                        source['A1'].comment = Comment('按隐藏页、门店编码和仓关联；同店同仓跨日有歧义时，请在导入中划分记录并选择订单，不按行号猜测。','补货助手')
                    else:
                        dest = book[source.title]
                    append_sheet(source, dest, 2 if index else 1, styles, cancel)
        identity.write(book, identities)
        progress('压缩保存'+title+'，完成后响应取消…')
        book.save(file)
    return file, identities

def export_returns(path,views,folder,mode,progress,cancel,*,stage='return'):
    from export_files import verify_identity
    completed=[]
    groups=[[v] for v in views] if mode=='separate' else [[v for v in views if v['head']['warehouse']==w] for w in dict.fromkeys(v['head']['warehouse'] for v in views)]
    try:
        for i,group in enumerate(groups):
            checkpoint(cancel);numbers={}
            for v in group:
                h=v['head']
                if h['number'] in numbers:raise ValueError('同一仓单多个修订不能合并为门店表，请分别导出，避免回传关联歧义')
                numbers[h['number']]=h['revision']
            with tempfile.TemporaryDirectory(prefix='.returned-',dir=folder) as temp:
                title='门店回传单' if stage=='return' else '运营签发单'
                if len(group) > 1:
                    temp_file, identities = merge_returns(path, group, temp, stage, numbers, progress, cancel)
                else:
                    temp_file = Path(temp)/'returned.xlsx'
                    with closing(returned_book(path, group[0], temp, progress, cancel, stage)) as book:
                        identities = identity.read(book)
                        identity.write(book, identities)
                        progress('压缩保存'+title+'，完成后响应取消…')
                        book.save(temp_file)
                verify_identity(temp_file, identities)
                name=title+'-'+group[0]['head']['warehouse']+'-'+(group[0]['head']['number'] if mode=='separate' else '多订单')+'-'+uuid.uuid4().hex[:8]+'.xlsx';target=Path(folder)/name
                checkpoint(cancel);os.link(temp_file,target);completed.append(str(target))
            progress(RowProgress('已导出'+title,i+1,len(groups)))
        return {'files':completed,'cancelled':False,'error':None}
    except Cancelled:return {'files':completed,'cancelled':True,'error':None}
    except Exception as exc:return {'files':completed,'cancelled':False,'error':str(exc)}
