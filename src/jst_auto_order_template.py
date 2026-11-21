"""Native 聚水潭 order XLS formats. Every output represents exactly one order."""
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from jst_allocation import number
from runtime_support import checkpoint


SHEET = '订单信息'
TEMPLATE = Path(__file__).with_name('jst-order-template.xls')
MAX_CELL = 32_767
MAX_BYTES = 3_000_000
MAX_DATA_ROWS = 30_000
MULTI = 'MultiCommodity'
SINGLE = 'SingleCommodity'
CONTACT = {
    'buyer': (2, '买家帐号'),
    'recipient': (11, '收货人姓名'),
    'phone': (12, '手机'),
    'province': (14, '省份'),
    'city': (15, '城市'),
    'district': (16, '区县'),
    'address': (17, '地址'),
}


def decimal_text(value):
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def cell_length(value):
    return len(value.encode('utf-16-le')) // 2


def order_lines(rows, cancel=None):
    """Use saved quantities and order; discard only the zero rows."""
    lines = []
    seen = set()
    total = amount = Decimal(0)
    chars = 0
    for row in rows:
        checkpoint(cancel)
        qty = number(row.get('quantity'), '已保存分配数量')
        if not qty:
            continue
        sku = row.get('sku')
        if (not isinstance(sku, str) or not sku.strip() or sku in seen
                or any(c in ',*' or ord(c) < 32 for c in sku)):
            raise ValueError('正数量商品编码为空、重复或包含多商品分隔符，请核对原生成单')
        seen.add(sku)
        price = number(row.get('price'), '已保存基本售价')
        line = [sku, decimal_text(qty), decimal_text(price)]
        chars += cell_length(f'{sku}*{line[1]}') + bool(lines)
        total += qty
        amount += qty * price
        lines.append(line)
    if not lines:
        raise ValueError('没有正数量商品')
    return lines, {'sku_count':len(lines),'quantity':decimal_text(total),
                   'amount':decimal_text(amount),'sku_cell_chars':chars}


def validate_phone(profile):
    phone = profile.get('phone')
    if not isinstance(phone, str) or len(phone) != 11 or not phone.isascii() or not phone.isdigit():
        raise ValueError('收货手机须为11位数字，请核对本仓配送资料')


def make_file(profile, online, lines, stats, *, template=TEMPLATE, import_format=MULTI, cancel=None):
    if import_format not in (MULTI,SINGLE):
        raise ValueError('未知聚水潭导入格式')
    if import_format == MULTI and stats['sku_cell_chars'] > MAX_CELL:
        raise ValueError(f"U 列商品串 {stats['sku_cell_chars']:,} 字符，超过 {MAX_CELL:,}；需核验可保留整单的其他格式")
    if not lines or (import_format == SINGLE and len(lines) > MAX_DATA_ROWS):
        raise ValueError(f'单张订单的明细超出聚水潭 {MAX_DATA_ROWS:,} 行限制或没有商品')
    values = {}
    for field, (column, title) in CONTACT.items():
        value = profile.get(field)
        if not isinstance(value,str) or not value.strip():
            raise ValueError('缺少'+title)
        if cell_length(value) > MAX_CELL:
            raise ValueError(title+'超过 Excel 单格长度')
        values[column] = value
    validate_phone(profile)
    values.update({0:online, 20:None, 25:'否', 29:'现场取货'})
    if import_format == MULTI:
        items = [(','.join(f'{sku}*{qty}' for sku,qty,_ in lines), None)]
    else:
        items = [(sku,qty) for sku,qty,_ in lines]
    if any(cell_length(sku) > MAX_CELL for sku,_ in items):
        raise ValueError('商品编码或多商品串超过 Excel 单格长度')

    import xlrd
    import xlwt
    from xlutils.copy import copy
    data = Path(template).read_bytes()
    source = xlrd.open_workbook(file_contents=data, formatting_info=True)
    base = source.sheet_by_name(SHEET)
    expected = {0:'线上订单号',20:'商品编码（必填）',23:'数量（必填）',
                24:'商品单价（必填）',25:'是否赠品',29:'快递公司',
                **{col:name+'（必填）' for col,name in CONTACT.values()}}
    if (source.sheet_names()!=[SHEET] or base.nrows!=1 or base.ncols!=39
            or any(base.cell_value(0,c)!=name for c,name in expected.items())):
        raise ValueError('聚水潭订单模板的工作表或表头已变化')
    book = copy(source)
    sheet = book.get_sheet(0)
    style = xlwt.easyxf(num_format_str='@')
    for index, (sku, qty) in enumerate(items, 1):
        checkpoint(cancel)
        # Keep the existing one-row template field order byte-compatible.
        for column,value in values.items():
            sheet.write(index,column,sku if column==20 else value,style)
        if qty is not None:
            native_quantity = float(qty)
            if Decimal(str(native_quantity)) != Decimal(qty):
                raise ValueError('该商品的数量无法按原精度写入聚水潭 XLS')
            sheet.write(index,23,native_quantity)
    with BytesIO() as target:
        book.save(target)
        content=target.getvalue()
    if len(content)>MAX_BYTES:
        raise ValueError(f'文件 {len(content):,} 字节，超过聚水潭 3 MB 限制')
    return content, sha256(data).hexdigest()
