"""Export the qualifying rows from one warehouse's current new-product view."""
from datetime import date
from pathlib import Path
from uuid import uuid4

from export_files import publish_new
from xlsx_exports import stream_book, write_table


DETAIL_HEADERS = ('SKU', '商品名', '首入库日期', '仓龄（天）', '商品状态', '仓可用数', '说明', '原件行号')
HEADERS = DETAIL_HEADERS + ('归属仓', '判定日期', '仓资料版本')
WIDTHS = dict(enumerate((24, 42, 15, 12, 13, 14, 32, 12, 13, 15, 21), start=1))
FORMATS = {'SKU': '@', '首入库日期': 'yyyy-mm-dd', '判定日期': 'yyyy-mm-dd',
           '仓龄（天）': '0', '原件行号': '0'}


def export_detail(path, rows, warehouse, day, revision):
    if not rows:
        raise ValueError('当前仓没有符合条件的新品可导出')
    path = Path(path).resolve()
    if not path.suffix:
        path = path.with_suffix('.xlsx')
    if path.suffix.lower() != '.xlsx':
        raise ValueError('请选择 .xlsx 文件')
    if path.exists():
        raise FileExistsError(path)

    decided = date.fromisoformat(day)
    values = ((sku, name, date.fromisoformat(first), age, status, stock, note, line,
               warehouse, decided, revision)
              for sku, name, first, age, status, stock, note, line in rows)
    temporary = path.with_name('.new-products-' + uuid4().hex + '.xlsx')
    try:
        with stream_book() as book:
            write_table(book, '新品明细', HEADERS, values, widths=WIDTHS, style='single',
                        formats=FORMATS, row_count=len(rows))
            book.save(temporary)
        publish_new(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path
