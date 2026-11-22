"""The two audited order-analysis layouts, using the native streaming writer.

Only frozen projection rows are read.  No catalog, source file, database, or
platform lookup is performed here.  Formula caches may be streamed to an OOXML
packager by setting ``book._analytics_formula_cache(name, coordinate, value)``.
Openpyxl alone does not persist cached formula results.
"""
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import warnings

from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.filters import AutoFilter
from openpyxl.worksheet.table import Table, TableColumn, TableStyleInfo

from analytics_identifiers import HEADERS as ID_HEADERS, cells as identifier_cells
from export_progress import rows as row_progress
from runtime_support import checkpoint
import analytics_lifecycle_scope as lifecycle_scope

NOT_RETAINED = '未留存'
PENDING = '待核'
NOT_APPLICABLE = '不适用'
ZERO_BASE = '基数为0'
_NUMBER_TYPES = (Decimal, int, float)
_METRIC_LABELS = (NOT_RETAINED, PENDING, NOT_APPLICABLE, ZERO_BASE)
DISPLAY_NOTE = ('0或0.0%表示已知为零。未留存表示缺少原字段。待核表示依据未齐。'
                '不适用表示无需此环节。基数为0时无法计算比率。')
STAGES = ('model', 'signed', 'return', 'operations', 'payment', 'receipt')
STAGE_LABELS = ('模型建议量', '签发量', '门店回传量', '运营回传量', '实际下单量', '实际收货量')
RATE_LABELS = ('签发率', '门店回传率', '运营修正率', '实际确认率',
               '大仓订单满足率', '实际收货率', '模型建议履行率', '补货建议履行率')
RATE_PAIRS = ((1, 0), (2, 1), (3, 2), (3, 1), (4, 3), (5, 4), (5, 0), (5, 1))
SUMMARY_HEADERS = ['订单日期', '订单编号', '门店', '供货仓', '必采状态', '订单状态',
                   *STAGE_LABELS, *RATE_LABELS, *ID_HEADERS[1:]]
ALLOCATION_HEADERS = ('分配时采用的仓可用库存', '本单分配前余额', '实际分配量', '缺口原因')
DETAIL_HEADERS = ['订单编号', '门店', 'SKU编码', '商品名称', '订单状态',
                  *STAGE_LABELS[:4], *ALLOCATION_HEADERS, *STAGE_LABELS[4:], *RATE_LABELS, '统一计价单价',
                  '模型建议货值', '签发货值', '门店回传货值', '运营回传货值', '实际下单货值', '实际收货货值',
                  '订单日期', '供货仓', '必采状态', '商品条码', '三级类目', '供货状态',
                  '门店现货', '近30天销量', '门店在途', '目标持有量', '箱规数', '最低持有量',
                  '门店补货建议更新时间', '签发参考仓库存', '供货商', *ID_HEADERS[1:]]
INTEGER, MONEY, PERCENT = '#,##0', '#,##0.00', '0.0%'
_FONT = 'Microsoft YaHei'
_STYLES = {
    'body': (Font(name=_FONT, size=10, color='FF263648'), PatternFill('solid', fgColor='FFFFFFFF')),
    'even': (Font(name=_FONT, size=10, color='FF263648'), PatternFill('solid', fgColor='FFF3F6FA')),
    'all': (Font(name=_FONT, size=10, color='FF263648', bold=True), PatternFill('solid', fgColor='FFEAF1F8')),
    'title': (Font(name=_FONT, size=16, color='FF233B59', bold=True), PatternFill()),
    'note': (Font(name=_FONT, size=10, color='FF263648'), PatternFill()),
    'header': (Font(name=_FONT, size=10, color='FFFFFFFF', bold=True), PatternFill('solid', fgColor='FF233B59')),
    'card_header': (Font(name=_FONT, size=10, color='FFFFFFFF', bold=True), PatternFill('solid', fgColor='FF207D77')),
    'card': (Font(name=_FONT, size=16, color='FF233B59', bold=True), PatternFill('solid', fgColor='FFEAF1F8')),
    'total': (Font(name=_FONT, size=12, color='FF263648', bold=True), PatternFill('solid', fgColor='FFF3F6FA')),
}


def _finite_number(value):
    if value is None or isinstance(value, bool) or value == '':
        return None
    try:
        value = Decimal(str(value))
        return value if value.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _number(value):
    value = _finite_number(value)
    return value if value is not None and value >= 0 else None


def _store(row):
    name, code = row.get('store_name'), row.get('store')
    if name not in (None, '') and code not in (None, ''):
        return f'{name}（{code}）'
    return _text(name if name not in (None, '') else code)


def _quantity(row, stage):
    return _number(row.get('quantities', {}).get(stage)) if row.get('valid', {}).get(stage) else None


def _quantity_cell(row, stage):
    quantity = _quantity(row, stage)
    if quantity is not None:
        return quantity
    state = row.get('states', {}).get(stage)
    if state == '无需此环节':
        return NOT_APPLICABLE
    raw = row.get('quantities', {}).get(stage)
    if raw in (None, '') and (stage in ('model', 'signed')
                             or state in ('未启用／未留存', '已确认', '聚水潭已客审')):
        return NOT_RETAINED
    return PENDING


def _text(value):
    return NOT_RETAINED if value is None or value == '' else str(value)


def _status(row, detail=False):
    status = row.get('order_status') or row.get('status')
    if not status:
        labels = ('签发', '门店回传', '运营回传', '实际下单', '实际收货')
        status = '；'.join(label + '：' + str(row.get('states', {}).get(stage) or '未留存')
                          for label, stage in zip(labels, STAGES[1:]))
    if detail:
        status += '；范围：' + str(row.get('scope') or '待核')
        if row.get('reasons'):
            status += '（' + '；'.join(map(str, row['reasons'])) + '）'
    return status


@dataclass(frozen=True)
class _Formula:
    text: str
    cached: object


def _rate_formula(numerator, denominator, value, base):
    numeric_base = isinstance(base, _NUMBER_TYPES)
    if numeric_base and base == 0:
        cached = ZERO_BASE
    elif NOT_APPLICABLE in (value, base):
        cached = NOT_APPLICABLE
    elif numeric_base and isinstance(value, _NUMBER_TYPES):
        cached = value / base
    else:
        cached = PENDING
    return _Formula(
        f'=IF(AND(ISNUMBER({denominator}),{denominator}=0),"{ZERO_BASE}",'
        f'IF(OR({numerator}="{NOT_APPLICABLE}",{denominator}="{NOT_APPLICABLE}"),"{NOT_APPLICABLE}",'
        f'IF(COUNT({numerator},{denominator})<2,"{PENDING}",{numerator}/{denominator})))', cached)


def _value_formula(quantity_cell, price_cell, quantity, price):
    cached = quantity
    if isinstance(quantity, _NUMBER_TYPES):
        cached = quantity * price if price is not None else NOT_RETAINED
    return _Formula(
        f'=IF(COUNT({quantity_cell},{price_cell})=2,{quantity_cell}*{price_cell},'
        f'IF(ISNUMBER({quantity_cell}),"{NOT_RETAINED}",IF({quantity_cell}="","{PENDING}",{quantity_cell})))',
        cached)


class _Sheet:
    def __init__(self, book, name, widths, cancel, *, wrap_columns=()):
        self.ws = book.create_sheet(name)
        self.ws.sheet_view.showGridLines = False
        self.cancel, self.count, self.width = cancel, 0, len(widths)
        self.wrap_columns = (5, 6, *wrap_columns)
        self.cache = getattr(book, '_analytics_formula_cache', None)
        # Style IDs belong to this workbook. Cache complete, write-once style
        # combinations here; never share them with a later export/workbook.
        self._styles = {}
        for col, width in enumerate(widths, 1):
            self.ws.column_dimensions[get_column_letter(col)].width = width

    def merge(self, reference):
        self.ws.merged_cells.add(reference)

    def row(self, values=(), kind='body', formats=None, height=25, overrides=None):
        checkpoint(self.cancel)
        self.count += 1
        self.ws.row_dimensions[self.count].height = height
        formats=formats or {};overrides=overrides or {}
        center=kind in ('header','card','total');header=kind=='header'
        cells = []
        for col, value in enumerate([*values, *([None] * max(0, self.width - len(values)))], 1):
            formula = isinstance(value, _Formula)
            actual = value.text if formula else float(value) if isinstance(value, Decimal) else value
            cell = WriteOnlyCell(self.ws, actual)
            if isinstance(actual, str) and not formula:
                cell.data_type = 's'  # Identifiers, names and attributes cannot inject Excel formulas.
            style = overrides.get(col, kind)
            numeric = formula or isinstance(actual, (int, float)) or (col in formats and actual in _METRIC_LABELS)
            horizontal = 'center' if center or style in ('card', 'total') else 'right' if numeric else None
            wrap = header or col in self.wrap_columns or (isinstance(actual, str) and '\n' in actual)
            number_format = formats.get(col, 'General')
            key = (style, horizontal, wrap, number_format)
            cached = self._styles.get(key)
            if cached is None:
                cell.font, cell.fill = _STYLES[style]
                cell.alignment = Alignment(vertical='center', wrap_text=wrap, horizontal=horizontal,
                                           indent=1 if horizontal == 'right' else 0)
                cell.number_format = number_format
                self._styles[key] = cell._style
            else:
                cell._style = cached
            if formula and callable(self.cache):
                self.cache(self.ws.title, f'{get_column_letter(col)}{self.count}', value.cached)
            cells.append(cell)
        self.ws.append(cells)

    def table(self, headers, header_row, name):
        ref = f'A{header_row}:{get_column_letter(len(headers))}{max(header_row, self.count)}'
        self.ws.auto_filter.ref = ref
        table = Table(displayName=name, ref=ref,
                      tableColumns=[TableColumn(id=i, name=h) for i, h in enumerate(headers, 1)],
                      autoFilter=AutoFilter(ref=ref),
                      tableStyleInfo=TableStyleInfo(name='TableStyleMedium2', showFirstColumn=False,
                                                   showLastColumn=False, showRowStripes=True, showColumnStripes=False))
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore', message='In write-only mode you must add table columns manually')
            self.ws.add_table(table)


class _Metrics:
    def __init__(self):
        self.quantity = [Decimal(0)] * 6
        self.value = [Decimal(0)] * 6
        self.skus = [set() for _ in STAGES]
        self.invalid_quantity = [False] * 6
        self.invalid_value = [False] * 6

    def add(self, row):
        if row.get('scope') != '纳入':
            if row.get('scope') != '排除':
                self.invalid_quantity = self.invalid_value = [True] * 6
            return
        price = _number(row.get('price'))
        for i, stage in enumerate(STAGES):
            quantity = _quantity(row, stage)
            if quantity is None:
                self.invalid_quantity[i] = self.invalid_value[i] = True
                continue
            self.quantity[i] += quantity
            if quantity > 0:
                self.skus[i].add(str(row['sku']))
            if price is None:
                self.invalid_value[i] = True
            else:
                self.value[i] += quantity * price

    def get(self, kind):
        bad = self.invalid_value if kind == 'value' else self.invalid_quantity
        values = self.value if kind == 'value' else self.quantity if kind == 'quantity' else [len(s) for s in self.skus]
        return [PENDING if unknown else value for value, unknown in zip(values, bad)]


def _orders(rows, cancel):
    orders = OrderedDict()
    for row in rows:
        checkpoint(cancel)
        key = tuple(row.get(k) for k in ('day', 'store', 'warehouse', 'order'))
        if key not in orders:
            orders[key] = {'first': row, 'groups': OrderedDict((g, _Metrics()) for g in ('必采', '非必采'))}
        order = orders[key]
        group = row.get('group') if row.get('group') in ('必采', '非必采') else '属性待核'
        if group not in order['groups']:
            order['groups'][group] = _Metrics()
        order['groups'][group].add(row)
    return list(orders.values())


def _sum(values):
    return PENDING if PENDING in values else sum(values, Decimal(0))


def _dashboard(sheet, title, total, last_row, note):
    """Rows 1-14 match the audited dashboard and eight-rate legend."""
    for reference in ('A2:L2', 'A3:L3', 'A13:L13', 'A14:L14', 'N5:T5'):
        sheet.merge(reference)
    for top, bottom in ((5, 6), (8, 9)):
        for left, right in (('A', 'C'), ('D', 'F'), ('G', 'I'), ('J', 'L')):
            sheet.merge(f'{left}{top}:{right}{top}')
            sheet.merge(f'{left}{bottom}:{right}{bottom}')
    for left, right in (('A', 'B'), ('C', 'D'), ('E', 'F'), ('G', 'H'), ('I', 'J'), ('K', 'L')):
        sheet.merge(f'{left}11:{right}11'); sheet.merge(f'{left}12:{right}12')
    for row in range(6, 14):
        sheet.merge(f'N{row}:O{row}'); sheet.merge(f'P{row}:T{row}')
    cards = {5: ('模型建议履行率', '补货建议履行率', '大仓订单满足率', '实际收货率'),
             8: RATE_LABELS[:4]}
    toprefs = ['A12', 'C12', 'E12', 'G12', 'I12', 'K12']
    cardpairs = {6: RATE_PAIRS[6:8] + (RATE_PAIRS[4], RATE_PAIRS[5]), 9: RATE_PAIRS[:4]}
    for row in range(1, 15):
        values = [None] * sheet.width
        overrides = {}
        formats = {}
        kind = 'note'
        if row == 2:
            values[0] = title; kind = 'title'
        elif row == 3:
            values[0] = '当前筛选范围 · 每笔订单分ALL、必采、非必采；有属性待核时另列 · 大盘仅汇总ALL'
        elif row in cards:
            for col, label in zip((1, 4, 7, 10), cards[row]):
                values[col - 1] = label
                for c in range(col, col + 3): overrides[c] = 'card_header' if row == 5 and col in (1, 4) else 'header'
        elif row in cardpairs:
            for col, (numerator, denominator) in zip((1, 4, 7, 10), cardpairs[row]):
                values[col - 1] = _rate_formula(toprefs[numerator], toprefs[denominator], total[numerator], total[denominator])
                for c in range(col, col + 3): overrides[c] = 'card'; formats[c] = PERCENT
        elif row == 11:
            for i, label in enumerate(STAGE_LABELS): values[i * 2] = label
            for c in range(1, 13): overrides[c] = 'header'
        elif row == 12:
            for i, col in enumerate('GHIJKL'):
                span = f'{col}17:{col}{max(17, last_row)}'
                criteria = f'$E$17:$E${max(17, last_row)}'
                values[i * 2] = _Formula(f'=IF(COUNTIFS({criteria},"ALL",{span},"{PENDING}")>0,"{PENDING}",SUMIFS({span},{criteria},"ALL"))', total[i])
                for c in (i * 2 + 1, i * 2 + 2): overrides[c] = 'total'; formats[c] = sheet.stage_format
        elif row == 13:
            values[0] = 'ALL为已纳入分组的六阶段合计；八率用合计分子÷合计分母，不平均百分比。属性待核仍计入ALL。'
        elif row == 14:
            values[0] = note + '\n' + DISPLAY_NOTE
        if row == 5:
            values[13] = '指标口径'
            for c in range(14, 21): overrides[c] = 'header'
        if 6 <= row <= 13:
            i = row - 6; numerator, denominator = RATE_PAIRS[i]
            values[13] = RATE_LABELS[i]
            values[15] = STAGE_LABELS[numerator] + ' ÷ ' + STAGE_LABELS[denominator]
        sheet.row(values, kind, formats, 52 if row == 14 else 32 if row in (6, 9) else 25, overrides)


def append_summary(book, report, rows, progress=lambda _: None, cancel=None):
    """Keep every selected order; exclude 排除, expose 待核 as unavailable."""
    progress = progress or (lambda _: None)
    orders = _orders(rows, cancel)
    row_count = sum(1 + len(order['groups']) for order in orders)
    names = []
    for index, (name, kind) in enumerate((('SKU种类', 'skus'), ('商品货值', 'value'), ('商品数量', 'quantity'))):
        checkpoint(cancel)
        totals = [_sum([_sum([group.get(kind)[i] for group in order['groups'].values()]) for order in orders]) for i in range(6)]
        sheet = _Sheet(book, name, [16, 18, 18] + [16] * 17 + [26, 30], cancel)
        sheet.stage_format = MONEY if kind == 'value' else INTEGER
        sheet.ws.freeze_panes = 'E17'; sheet.ws.print_title_rows = '15:16'
        _dashboard(sheet, '补货订单完成情况 · ' + name, totals, 16 + row_count,
                   '顶部只取ALL；排除不入统计、待核不当零。SKU按订单内计数，跨单分别加；数量为各库存单位执行量相加，非同单位物量。生命周期范围：'+lifecycle_scope.label(report.get('query',{})))
        for reference in ('A15:F15', 'G15:L15', 'M15:R15', 'S15:T15', 'U15:V15'): sheet.merge(reference)
        sheet.row(['订单与必采状态', None, None, None, None, None, '六阶段 · ' + name, None, None, None, None, None,
                   '环节转化', None, None, None, None, None, '最终履行', None, '外部订单号'], 'header',
                  overrides={19: 'card_header', 20: 'card_header'})
        sheet.row(SUMMARY_HEADERS, 'header', height=42)
        row_progress(progress, name, 0, row_count)
        for order in orders:
            checkpoint(cancel)
            first = order['first']; groups = order['groups']; start = sheet.count + 1
            group_values = [g.get(kind) for g in groups.values()]
            all_values = [_sum([v[i] for v in group_values]) for i in range(6)]
            ids = identifier_cells([first])[1:]
            for offset, (group, metrics) in enumerate([('ALL', all_values), *zip(groups, group_values)]):
                current = start + offset
                stage_values = metrics if offset else [_Formula(f'=IF(COUNT({c}{start + 1}:{c}{start + len(groups)})<{len(groups)},"{PENDING}",SUM({c}{start + 1}:{c}{start + len(groups)}))', metrics[i]) for i, c in enumerate('GHIJKL')]
                rates = [_rate_formula(f'{get_column_letter(7 + ni)}{current}', f'{get_column_letter(7 + di)}{current}',
                                       metrics[ni], metrics[di]) for ni, di in RATE_PAIRS]
                identity = [first.get('day'), identifier_cells([first])[0], _store(first), first.get('warehouse'), group, _status(first)]
                sheet.row([*identity, *stage_values, *rates, *ids], 'all' if not offset else 'body' if offset % 2 else 'even',
                          {**{c: sheet.stage_format for c in range(7, 13)}, **{c: PERCENT for c in range(13, 21)}}, height=48)
            row_progress(progress, name, sheet.count - 16, row_count)
        sheet.table(SUMMARY_HEADERS, 16, f'AnalyticsOrders_{index}')
        names.append(name)
    return names


def retain_detail_row(row):
    return row.get('scope') != '排除' and (_quantity(row, 'model') != 0 or any(
        (_quantity(row, stage) or 0) > 0 for stage in STAGES[1:]))


def select_detail_rows(rows, cancel=None):
    selected = []
    for row in rows:
        checkpoint(cancel)
        if retain_detail_row(row):
            selected.append(row)
    return selected


def append_detail(book, report, rows, progress=lambda _: None, cancel=None):
    """Write the selected rows and their saved allocation facts."""
    progress = progress or (lambda _: None)
    widths = [18, 18, 16, 22] + [16] * 5 + [24, 20, 16, 56] + [16] * 20 + [20] + [16] * 8 + [24, 16, 27, 26, 30]
    sheet = _Sheet(book, 'SKU完整明细', widths, cancel, wrap_columns=(13,))
    sheet.ws.freeze_panes = 'E8'; sheet.ws.print_title_rows = '6:7'
    for reference in ('A2:R2', 'A3:R3', 'A4:R4', 'A5:R5', 'A6:E6', 'F6:I6', 'J6:M6', 'N6:O6',
                      'P6:U6', 'V6:W6', 'X6:AD6', 'AE6:AJ6', 'AK6:AQ6', 'AR6:AS6', 'AT6:AU6'):
        sheet.merge(reference)
    sheet.row(); sheet.row(['补货SKU生命周期明细'], 'title')
    sheet.row(['每行对应一张仓单的一个SKU。排除行省略。模型建议为0时，签发至实收有任一阶段确认数量大于0就保留。生命周期范围为'+lifecycle_scope.label(report.get('query',{}))+'。'], 'note')
    sheet.row(['种类按阶段数量>0计1；货值=数量×签发冻结V单价。新口径目标持有量为趋势后需求加安全库存。旧签发按当时口径展示；数量非同单位物量。'
               '库存分配读取当时的仓单记录。'], 'note')
    sheet.row(['比率＝对应后阶段数量÷前阶段数量。' + DISPLAY_NOTE], 'note')
    groups = [None] * len(DETAIL_HEADERS)
    for col, label in ((1, '订单与商品'), (6, '建议与回传数量'), (10, '库存分配'), (14, '下单与收货数量'),
                       (16, '环节转化'), (22, '最终履行'), (24, '统一单价与六阶段货值'), (31, '订单与商品属性'),
                       (37, '库存与订货约束'), (44, '签发参考与供货商'), (46, '外部订单号')):
        groups[col - 1] = label
    sheet.row(groups, 'header', overrides={22: 'card_header', 23: 'card_header'}); sheet.row(DETAIL_HEADERS, 'header', height=42)
    total = len(rows) if hasattr(rows, '__len__') else None
    if total is not None: row_progress(progress, sheet.ws.title, 0, total)
    stage_cols = [DETAIL_HEADERS.index(label) + 1 for label in STAGE_LABELS]
    stage_letters = [get_column_letter(col) for col in stage_cols]
    price_letter = get_column_letter(DETAIL_HEADERS.index('统一计价单价') + 1)
    formats = {**dict.fromkeys(stage_cols, INTEGER), **dict.fromkeys(range(16, 24), PERCENT),
               **dict.fromkeys(range(24, 31), MONEY), **dict.fromkeys((*range(37, 43), 44), 'General')}
    last_order=None;last_reference=None;ids=None
    for index, row in enumerate(rows, 1):
        checkpoint(cancel)
        current = sheet.count + 1
        displayed = [_quantity_cell(row, stage) for stage in STAGES]
        price = _number(row.get('price'))
        quantity_cells = [f'{column}{current}' for column in stage_letters]
        price_cell = f'{price_letter}{current}'
        rates = [_rate_formula(quantity_cells[ni], quantity_cells[di], displayed[ni], displayed[di])
                 for ni, di in RATE_PAIRS]
        amounts = [_value_formula(cell, price_cell, quantity, price)
                   for cell, quantity in zip(quantity_cells, displayed)]
        allocation = row.get('allocation_detail') or {}
        saved_numbers = [_finite_number(allocation.get(key)) for key in ('stock', 'before', 'quantity')]
        reason = allocation.get('reason')
        if not allocation and row.get('states', {}).get('allocation') == '待重新分配':
            reason = '待重新分配'
        allocation_cells = [v if v is not None else NOT_RETAINED for v in saved_numbers]
        allocation_cells.append(reason if isinstance(reason, str) and reason.strip() else NOT_RETAINED)
        attrs = row.get('template_attributes') or {}
        def number(key):
            # Source attributes are evidence, including genuine negative stock
            # or net sales.  Only stage quantities and prices reject negatives.
            value = _finite_number(attrs.get(key))
            return value if value is not None else NOT_RETAINED
        reference=row.get('references')
        if ids is None or row['order']!=last_order or reference is not last_reference:
            ids=identifier_cells([row]);last_order=row['order'];last_reference=reference
        values = [ids[0], _store(row), _text(row.get('sku')), _text(row.get('name')), _status(row, True),
                  *displayed[:4], *allocation_cells, *displayed[4:], *rates, price if price is not None else NOT_RETAINED, *amounts,
                  _text(row.get('day')), _text(row.get('warehouse')), _text(row.get('group')),
                  _text(attrs.get('barcode')), _text(row.get('product_category')), _text(attrs.get('supply_state')),
                  *[number(key) for key in ('stock', 'monthly_sales', 'in_transit', 'target_holding', 'pack_size', 'minimum_holding')],
                  _text(attrs.get('stock_snapshot_at')), number('warehouse_stock'), _text(row.get('upstream_supplier')), *ids[1:]]
        sheet.row(values,'body' if index%2 else 'even',formats,height=48)
        if index % 500 == 0 and total is not None: row_progress(progress, sheet.ws.title, index, total)
    sheet.table(DETAIL_HEADERS, 7, 'AnalyticsSkuLifecycle')
    row_progress(progress, sheet.ws.title, sheet.count - 7, total if total is not None else sheet.count - 7)
    return [sheet.ws.title]
