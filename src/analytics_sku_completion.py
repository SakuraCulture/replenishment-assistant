"""SKU fulfillment over the same frozen warehouse-order lines as the detail export."""
from dataclasses import dataclass, field
from decimal import Decimal

from analytics_template_export import INTEGER, PENDING, _Formula, _Sheet, _quantity
from export_progress import rows as row_progress
from runtime_support import checkpoint


HEADERS = ('SKU编码', '商品名称', '门店总订单需求数量', '实际下单数量', '完成率', '需求订单数', '需求门店数')
PERCENT = '0.00%'


@dataclass
class _Sku:
    name: str = ''
    name_day: str = ''
    demand: Decimal | None = Decimal(0)
    ordered: Decimal | None = Decimal(0)
    demand_orders: set = field(default_factory=set)
    demand_stores: set = field(default_factory=set)

    def add(self, row):
        name = str(row['name']) if row.get('name') is not None else ''
        if name.strip() and (not self.name or row['day'] > self.name_day):
            self.name, self.name_day = name, row['day']
        included = row['scope'] == '纳入'
        demand = _quantity(row, 'return') if included else None
        ordered = _quantity(row, 'payment') if included else None
        self.demand = _add(self.demand, demand)
        self.ordered = _add(self.ordered, ordered)
        if demand is not None and demand > 0:
            self.demand_orders.add(row['order'])
            self.demand_stores.add(row['store'])


def _add(total, value):
    return None if total is None or value is None else total + value


def _rate(row, demand, ordered):
    if demand == 0:
        cached = ''
    elif demand is None or ordered is None:
        cached = PENDING
    else:
        cached = ordered / demand
    return _Formula(f'=IF(AND(ISNUMBER(C{row}),C{row}=0),"",'
                    f'IF(COUNT(C{row}:D{row})=2,D{row}/C{row},"{PENDING}"))', cached)


def _sum_cell(col, count, total):
    if not count:
        return 0
    area = f'{col}8:{col}{7 + count}'
    return _Formula(f'=IF(COUNT({area})=ROWS({area}),SUM({area}),"{PENDING}")',
                    PENDING if total is None else total)


def _qty_format(value):
    return '#,##0.########' if value is not None and value != value.to_integral_value() else INTEGER


def append_sku_completion(book, report, rows, progress, cancel):
    """Rows have already passed the export's order scope and lifecycle detail filter."""
    by_sku = {}
    stores, orders = set(), set()
    progress('汇总各 SKU 的门店回传量和实际下单量…')
    for row in rows:
        checkpoint(cancel)
        sku = row['sku']
        if sku not in by_sku:
            by_sku[sku] = _Sku()
        by_sku[sku].add(row)
        stores.add(row['store'])
        orders.add(row['order'])

    # Match the detail order for names tied on the latest day, and the sample's
    # text-SKU tie break for equal demand. Unresolved demand sorts last.
    ranked = sorted(by_sku.items(), key=lambda item: (
        item[1].demand is None, -(item[1].demand or 0), item[0]))
    demand = ordered = Decimal(0)
    for _, item in ranked:
        demand = _add(demand, item.demand)
        ordered = _add(ordered, item.ordered)
    sheet = _Sheet(book, 'SKU汇总', [24, 76, 24, 20, 16, 16, 16], cancel)
    sheet.ws.freeze_panes = 'A8'
    sheet.ws.print_title_rows = '7:7'
    for row in (1, 2, 3, 4):
        sheet.merge(f'A{row}:G{row}')
    sheet.row(['SKU 订单完成率汇总'], 'title', height=35)
    query = report['query']
    sheet.row([f"{query['from']} 至 {query['to']}  ·  {len(stores):,} 家门店  ·  "
               f"{len(orders):,} 张仓单  ·  {len(ranked):,} 个 SKU"], 'note')
    sheet.row(['门店需求取门店回传量。完成率 = 实际下单数量 ÷ 门店总订单需求数量。需求为 0 时留空。'], 'note')
    sheet.row(['需求订单数按有正需求的仓单去重，需求门店数按门店编码去重。资料待核时，对应指标及其合计显示“待核”。'], 'note')
    sheet.row(['所有 SKU 合计', None, _sum_cell('C', len(ranked), demand), _sum_cell('D', len(ranked), ordered),
               _rate(5, demand, ordered)], 'total', {3: _qty_format(demand), 4: _qty_format(ordered), 5: PERCENT})
    sheet.row(height=10)
    sheet.row(HEADERS, 'header', height=32)
    for index, (sku, item) in enumerate(ranked, 1):
        demand_known = item.demand is not None
        sheet.row([sku, item.name or '未留存', item.demand if demand_known else PENDING,
                   PENDING if item.ordered is None else item.ordered, _rate(index + 7, item.demand, item.ordered),
                   len(item.demand_orders) if demand_known else PENDING, len(item.demand_stores) if demand_known else PENDING],
                  'body' if index % 2 else 'even',
                  {1: '@', 3: _qty_format(item.demand), 4: _qty_format(item.ordered), 5: PERCENT}, height=22)
        if index % 500 == 0 or index == len(ranked):
            row_progress(progress, sheet.ws.title, index, len(ranked))
    sheet.table(HEADERS, 7, 'SkuOrders')
    return [sheet.ws.title]
