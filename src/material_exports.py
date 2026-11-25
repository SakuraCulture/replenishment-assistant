"""Export the selected material warehouse revision with its saved allocation basis."""
import hashlib
import os
import re
import tempfile
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import openpyxl

import order_identity
import purchases
import warehouse_execution
import work_repository as wr
from runtime_support import checkpoint


def export(path, repo, head, folder, progress, cancel):
    with closing(purchases.connect(path)) as db:
        db.execute('BEGIN')
        purchases.validate_head(db,head)
        parent = db.execute("SELECT day,store,number FROM normal_orders WHERE oid=? AND kind='material'", (head['oid'],)).fetchone()
        if not parent:
            raise wr.Conflict('原物料仓单未找到')
        from warehouse_orders import child_number
        if (*parent[:2],child_number(parent[2],head['warehouse'])) != (head['day'],head['store'],head['number']):
            raise wr.Conflict('原物料仓单身份变化，请刷新')
        order = warehouse_execution.get_order(repo,head,db=db,progress=progress)
    result = order['payload']['result']
    snapshot = [row['_material'] for row in result['rows']]
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',head['number']).rstrip(' .')
    target = Path(folder)/(filename+f"_V{head['revision']:03d}_物料单.xlsx")
    if target.is_file():
        with target.open('rb') as stream:
            sha = hashlib.file_digest(stream,'sha256').hexdigest()
        with closing(purchases.connect(path)) as db:
            found = db.execute('''SELECT 1 FROM normal_events WHERE oid=? AND revision=? AND action='导出物料单'
                AND json_extract(detail,'$.path')=? AND json_extract(detail,'$.sha256')=?
                AND json_extract(detail,'$.warehouse')=?''',(head['oid'],head['revision'],str(target),sha,head['warehouse'])).fetchone()
            if found:
                purchases.validate_head(db,head)
                return str(target)
    book = openpyxl.Workbook()
    main = book.active
    main.title = '物料单'
    main.append(('仓单号','门店编码','门店名称','供货仓','业务日期','商品编码','商品名','库存单位','实分数量','单价','金额','仓资料版本'))
    evidence = book.create_sheet('申请来源')
    evidence.append(('商品编码','申请编号','申请版本','原申请量','核对量','实分量','原件','原行','备注'))
    for row in snapshot:
        checkpoint(cancel)
        main.append((head['number'],head['store'],result['store_name'],head['warehouse'],head['day'],row['sku'],row['name'],row['unit'],
                     Decimal(row['quantity']),Decimal(row['price']),Decimal(row['amount']),row['catalog_revision']))
        for claim in row['claims']:
            evidence.append((row['sku'],claim['request'],claim['version'],Decimal(claim['requested']),Decimal(claim['demand']),
                             Decimal(claim['quantity']),claim['source'].get('name'),claim['source'].get('row'),claim['note']))
    for sheet in (main,evidence):
        sheet.freeze_panes='A2'
        sheet.auto_filter.ref=sheet.dimensions
        for cells in sheet.iter_rows():
            for cell in cells:
                if isinstance(cell.value,str):cell.data_type='s'
    order_identity.write(book,[order_identity.entry(dict(head,saved=order['saved']),head['warehouse'],main.title)])
    fd, temporary = tempfile.mkstemp(dir=target.parent,prefix='.物料单-',suffix='.xlsx')
    os.close(fd)
    temporary = Path(temporary)
    try:
        book.save(temporary)
        with temporary.open('rb') as stream:
            sha = hashlib.file_digest(stream,'sha256').hexdigest()
        checkpoint(cancel)
        with closing(purchases.connect(path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            purchases.validate_head(db,head)
            os.link(temporary,target)
            db.execute('INSERT INTO normal_events(at,oid,revision,action,detail) VALUES(?,?,?,?,?)',
                       (wr.now(),head['oid'],head['revision'],'导出物料单',wr.dumps(dict(path=str(target),sha256=sha,warehouse=head['warehouse']))))
    finally:
        book.close()
        temporary.unlink(missing_ok=True)
    return str(target)
