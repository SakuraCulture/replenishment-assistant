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

NA = 'n.a.'
STAGES = ('model', 'signed', 'return', 'operations', 'payment', 'receipt')
STAGE_LABELS = ('模型建议量', '签发量', '门店回传量', '运营回传量', '实际下单量', '实际收货量')
RATE_LABELS = ('签发率', '门店回传率', '运营修正率', '实际确认率',
               '大仓订单满足率', '实际收货率', '模型建议履行率', '补货建议履行率')
RATE_PAIRS = ((1, 0), (2, 1), (3, 2), (3, 1), (4, 3), (5, 4), (5, 0), (5, 1))
SUMMARY_HEADERS = ['订单日期', '订单编号', '门店', '供货仓', '必采状态', '订单状态',
                   *STAGE_LABELS, *RATE_LABELS, *ID_HEADERS[1:]]
DETAIL_HEADERS = ['订单编号', '门店', 'SKU编码', '商品名称', '订单状态',
                  *STAGE_LABELS, *RATE_LABELS, '统一计价单价',
                  '模型建议货值', '签发货值', '门店回传货值', '运营回传货值', '实际下单货值', '实际收货货值',
                  '订单日期', '供货仓', '必采状态', '商品条码', '三级类目', '供货状态',
                  '门店现货', '近30天销量', '门店在途', '目标持有量', '箱规数', '最低持有量',
                  '库存快照时间', '大仓可用库存', '供货商', *ID_HEADERS[1:]]
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


def _text(value):
    return NA if value is None or value == '' else str(value)


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


def _rate(numerator, denominator):
    return numerator / denominator if isinstance(numerator, (Decimal, int, float)) and isinstance(denominator, (Decimal, int, float)) and denominator != 0 else NA


@dataclass(frozen=True)
class _Formula:
    text: str
    cached: object


def _rate_formula(numerator, denominator, value):
    return _Formula(f'=IF(OR(COUNT({numerator},{denominator})<2,{denominator}=0),"{NA}",{numerator}/{denominator})', value)


class _Sheet:
    def __init__(self, book, name, widths, cancel):
        self.ws = book.create_sheet(name)
        self.ws.sheet_view.showGridLines = False
        self.cancel, self.count, self.width = cancel, 0, len(widths)
        self.cache = getattr(book, '_analytics_formula_cache', None)
        for col, width in enumerate(widths, 1):
            self.ws.column_dimensions[get_column_letter(col)].width = width

    def merge(self, reference):
        self.ws.merged_cells.add(reference)

    def row(self, values=(), kind='body', formats=None, height=25, overrides=None):
        checkpoint(self.cancel)
        self.count += 1
        self.ws.row_dimensions[self.count].height = height
        cells = []
        for col, value in enumerate([*values, *([None] * max(0, self.width - len(values)))], 1):
            formula = isinstance(value, _Formula)
            actual = value.text if formula else float(value) if isinstance(value, Decimal) else value
            cell = WriteOnlyCell(self.ws, actual)
            if isinstance(actual, str) and not formula:
                cell.data_type = 's'  # Identifiers, names and attributes cannot inject Excel formulas.
            style = (overrides or {}).get(col, kind)
            cell.font, cell.fill = _STYLES[style]
            numeric = formula or isinstance(actual, (int, float)) or (actual == NA and col in (formats or {}))
            horizontal = 'center' if kind in ('header', 'card', 'total') or style in ('card', 'total') else 'right' if numeric else None
            cell.alignment = Alignment(vertical='center', wrap_text=(kind == 'header' or col in (5, 6) or (isinstance(actual, str) and '\n' in actual)),
                                       horizontal=horizontal, indent=1 if horizontal == 'right' else 0)
            cell.number_format = (formats or {}).get(col, 'General')
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
        return [NA if unknown else value for value, unknown in zip(values, bad)]


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
    return NA if any(v == NA for v in values) else sum(values, Decimal(0))


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
                values[col - 1] = _rate_formula(toprefs[numerator], toprefs[denominator], _rate(total[numerator], total[denominator]))
                for c in range(col, col + 3): overrides[c] = 'card'; formats[c] = PERCENT
        elif row == 11:
            for i, label in enumerate(STAGE_LABELS): values[i * 2] = label
            for c in range(1, 13): overrides[c] = 'header'
        elif row == 12:
            for i, col in enumerate('GHIJKL'):
                span = f'{col}17:{col}{max(17, last_row)}'
                criteria = f'$E$17:$E${max(17, last_row)}'
                values[i * 2] = _Formula(f'=IF(COUNTIFS({criteria},"ALL",{span},"{NA}")>0,"{NA}",SUMIFS({span},{criteria},"ALL"))', total[i])
                for c in (i * 2 + 1, i * 2 + 2): overrides[c] = 'total'; formats[c] = sheet.stage_format
        elif row == 13:
            values[0] = 'ALL为已纳入分组的六阶段合计；八率用合计分子÷合计分母，不平均百分比。属性待核仍计入ALL。'
        elif row == 14:
            values[0] = note
        if row == 5:
            values[13] = '指标口径'
            for c in range(14, 21): overrides[c] = 'header'
        if 6 <= row <= 13:
            i = row - 6; numerator, denominator = RATE_PAIRS[i]
            values[13] = RATE_LABELS[i]
            values[15] = STAGE_LABELS[numerator] + ' ÷ ' + STAGE_LABELS[denominator]
        sheet.row(values, kind, formats, 32 if row in (6, 9) else 25, overrides)


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
                   '顶部只取ALL；排除不入统计、待核不当零。SKU按订单内计数，跨单分别加；数量为各库存单位执行量相加，非同单位物量。')
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
                stage_values = metrics if offset else [_Formula(f'=IF(COUNT({c}{start + 1}:{c}{start + len(groups)})<{len(groups)},"{NA}",SUM({c}{start + 1}:{c}{start + len(groups)}))', metrics[i]) for i, c in enumerate('GHIJKL')]
                rates = [_rate_formula(f'{get_column_letter(7 + ni)}{current}', f'{get_column_letter(7 + di)}{current}', _rate(metrics[ni], metrics[di])) for ni, di in RATE_PAIRS]
                identity = [first.get('day'), identifier_cells([first])[0], _store(first), first.get('warehouse'), group, _status(first)]
                sheet.row([*identity, *stage_values, *rates, *ids], 'all' if not offset else 'body' if offset % 2 else 'even',
                          {**{c: sheet.stage_format for c in range(7, 13)}, **{c: PERCENT for c in range(13, 21)}}, height=48)
            row_progress(progress, name, sheet.count - 16, row_count)
        sheet.table(SUMMARY_HEADERS, 16, f'AnalyticsOrders_{index}')
        names.append(name)
    return names


def append_detail(book, report, rows, progress=lambda _: None, cancel=None):
    """Append all selected scope rows to the 43-column lifecycle detail page."""
    progress = progress or (lambda _: None)
    sheet = _Sheet(book, 'SKU完整明细', [18, 18, 16, 22] + [16] * 25 + [20] + [16] * 8 + [24, 16, 27, 26, 30], cancel)
    sheet.ws.freeze_panes = 'E8'; sheet.ws.print_title_rows = '6:7'
    for reference in ('A2:N2', 'A3:N3', 'A4:N4', 'A6:E6', 'F6:K6', 'L6:Q6', 'R6:S6', 'T6:Z6', 'AA6:AF6', 'AG6:AM6', 'AN6:AO6', 'AP6:AQ6'): sheet.merge(reference)
    sheet.row(); sheet.row(['补货SKU生命周期明细'], 'title')
    sheet.row(['一行＝订单×供货仓×SKU；保留本筛选全部纳入、待核、排除行；同SKU跨订单分别计数。'], 'note')
    sheet.row(['种类按阶段数量>0计1；货值=数量×签发冻结V单价。目标持有量为当时预测需求含安全库存，不按签发量倒推；数量非同单位物量。'], 'note')
    sheet.row()
    groups = [None] * 43
    for col, label in ((1, '订单与商品'), (6, '六阶段数量'), (12, '环节转化'), (18, '最终履行'),
                       (20, '统一单价与六阶段货值'), (27, '订单与商品属性'), (33, '库存与订货约束'), (40, '大仓供给'), (42, '外部订单号')):
        groups[col - 1] = label
    sheet.row(groups, 'header', overrides={18: 'card_header', 19: 'card_header'}); sheet.row(DETAIL_HEADERS, 'header', height=42)
    total = len(rows) if hasattr(rows, '__len__') else None
    if total is not None: row_progress(progress, sheet.ws.title, 0, total)
    for index, row in enumerate(rows, 1):
        checkpoint(cancel)
        current = sheet.count + 1
        quantities = [_quantity(row, stage) for stage in STAGES]
        displayed = [q if q is not None else NA for q in quantities]
        price = _number(row.get('price'))
        rates = [_rate_formula(f'{get_column_letter(6 + ni)}{current}', f'{get_column_letter(6 + di)}{current}', _rate(displayed[ni], displayed[di])) for ni, di in RATE_PAIRS]
        amounts = [_Formula(f'=IF(COUNT({get_column_letter(6 + i)}{current},T{current})<2,"{NA}",{get_column_letter(6 + i)}{current}*T{current})', q * price if q is not None and price is not None else NA) for i, q in enumerate(quantities)]
        attrs = row.get('template_attributes') or {}
        def number(key):
            # Source attributes are evidence, including genuine negative stock
            # or net sales.  Only stage quantities and prices reject negatives.
            value = _finite_number(attrs.get(key))
            return value if value is not None else NA
        ids = identifier_cells([row])
        values = [ids[0], _store(row), _text(row.get('sku')), _text(row.get('name')), _status(row, True),
                  *displayed, *rates, price if price is not None else NA, *amounts,
                  _text(row.get('day')), _text(row.get('warehouse')), _text(row.get('group')),
                  _text(attrs.get('barcode')), _text(row.get('product_category')), _text(attrs.get('supply_state')),
                  *[number(key) for key in ('stock', 'monthly_sales', 'in_transit', 'target_holding', 'pack_size', 'minimum_holding')],
                  _text(attrs.get('stock_snapshot_at')), number('warehouse_stock'), _text(row.get('upstream_supplier')), *ids[1:]]
        sheet.row(values, 'body' if index % 2 else 'even',
                  {**{c: INTEGER for c in range(6, 12)}, **{c: PERCENT for c in range(12, 20)}, **{c: MONEY for c in range(20, 27)},
                   **{c: 'General' for c in (*range(33, 39), 40)}}, height=48)
        if index % 500 == 0 and total is not None: row_progress(progress, sheet.ws.title, index, total)
    sheet.table(DETAIL_HEADERS, 7, 'AnalyticsSkuLifecycle')
    row_progress(progress, sheet.ws.title, sheet.count - 7, total if total is not None else sheet.count - 7)
    return [sheet.ws.title]
