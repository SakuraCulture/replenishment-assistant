"""Operating daily report calculations over one business day's frozen inputs."""
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from runtime_support import checkpoint


VERSION = '经营日报-1'
RULE = '河北缺码且配置山东从仓才回退（B15）'
WAREHOUSES = ('山东仓', '河北仓')
EXCLUDED_TAGS = {'', '无标签', '暂停补货', '半闭店'}
STORE_HEADERS = ('门店编码', '门店名称', '标签', '运营负责人', '主仓', '动销数', '动销缺货数',
                 '原始缺货率', '剔除后缺货数', '剔除后缺货率', '大仓缺货数', '缺货且在途',
                 '地采动销数', '地采缺货数', '地采缺货率', '全量动销数', '全量缺货数', '全量缺货率',
                 '明细行数', '月销合计')
DETAIL_HEADERS = ('一级类目', '供应商', '门店编码', '门店名称', '标签', '商品编码', '商品名称',
                  '补货状态', '月销', '可用库存', '在途库存', '是否大仓(O)', '是否地采',
                  '是否地采黑名单(P)', '计入动销(Q)', '大仓缺货(R)', '是否大仓缺货')


class PendingInputs(ValueError):
    def __init__(self, issues):
        self.issues = list(issues)
        super().__init__('\n'.join(self.issues))


def decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def average(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def classify(store, sku, catalogs):
    """Membership precedes stock attribution. A disabled match is still a match."""
    warehouse = store['primary']
    product = catalogs[warehouse].get(sku)
    role = '主仓'
    if warehouse == '河北仓' and product is None and store['secondary'] == '山东仓':
        warehouse, role = '山东仓', '从仓'
        product = catalogs[warehouse].get(sku)
    if product is None:
        return None, None, None
    if product['status'] == '禁用' or '标杆淘汰' in product['virtual']:
        return warehouse + '禁采', None, None
    return f'{role}（{warehouse[:2]}）', warehouse, product


def stockout_sets(catalogs):
    return {warehouse: {sku for sku, p in products.items()
                        if not p['virtual'] and p['status'] == '启用' and p['stock'] is not None
                        and p['stock'] <= 0 and (warehouse != '山东仓' or p['category'] != '日配')}
            for warehouse, products in catalogs.items()}


@dataclass
class Counts:
    rows: int = 0
    sales: Decimal = Decimal(0)
    active: int = 0
    shortage: int = 0
    warehouse_shortage: int = 0
    inbound: int = 0
    inbound_unknown: int = 0
    local_active: int = 0
    local_shortage: int = 0
    all_active: int = 0
    all_shortage: int = 0

    @property
    def remaining(self):
        return self.shortage - self.warehouse_shortage

    def rates(self):
        return (ratio(self.all_shortage, self.all_active), ratio(self.remaining, self.active),
                ratio(self.shortage, self.active), ratio(self.local_shortage, self.local_active))


@dataclass
class Cut:
    stores: set = field(default_factory=set)
    skus: set = field(default_factory=set)
    active: int = 0
    shortage: int = 0
    warehouse_shortage: int = 0

    def row(self, code, sku, shortage, warehouse_shortage):
        self.stores.add(code)
        self.skus.add(sku)
        self.active += 1
        self.shortage += shortage
        self.warehouse_shortage += warehouse_shortage

    def store(self, code, counts):
        self.stores.add(code)
        self.active += counts.active
        self.shortage += counts.shortage
        self.warehouse_shortage += counts.warehouse_shortage

    def values(self):
        remaining = self.shortage - self.warehouse_shortage
        return (len(self.stores), self.active, self.shortage, ratio(self.shortage, self.active),
                remaining, ratio(remaining, self.active), self.warehouse_shortage)

    def add(self, other):
        self.stores.update(other.stores)
        self.skus.update(other.skus)
        self.active += other.active
        self.shortage += other.shortage
        self.warehouse_shortage += other.warehouse_shortage


@dataclass
class Fulfillment:
    stores: set = field(default_factory=set)
    skus: set = field(default_factory=set)
    demand: Decimal = Decimal(0)
    demand_value: Decimal | None = Decimal(0)
    ordered: Decimal = Decimal(0)
    ordered_value: Decimal | None = Decimal(0)
    pending: int = 0

    def add(self, row, numbers):
        demand, demand_value, ordered, ordered_value = numbers
        if demand is None or ordered is None:
            self.pending += 1
            return
        if demand <= 0 and ordered <= 0:
            return
        self.stores.add(row['store'])
        self.skus.add(row['sku'])
        self.demand += demand
        self.ordered += ordered
        self.demand_value = (self.demand_value + demand_value
                             if self.demand_value is not None and demand_value is not None else None)
        self.ordered_value = (self.ordered_value + ordered_value
                              if self.ordered_value is not None and ordered_value is not None else None)

    def values(self):
        return (len(self.skus), len(self.stores), self.demand, self.demand_value, self.ordered,
                self.ordered_value, ratio(self.ordered, self.demand))


def fulfillment(rows, catalogs, owners, cancel=None):
    supplier, buyer = defaultdict(Fulfillment), defaultdict(Fulfillment)
    unmapped = []
    seen = set()
    for row in rows:
        checkpoint(cancel)
        identity = (row['warehouse'], row['order'], row['revision'], row['sku'])
        if identity in seen:
            raise ValueError('订单来源有重复商品行：' + ' / '.join(map(str, identity)))
        seen.add(identity)
        warehouse = row['warehouse']
        product = catalogs[warehouse].get(row['sku'], {})
        vendor = row.get('supplier') or product.get('supplier') or '(未填报)'
        category = row.get('category') or product.get('category') or ''
        values = tuple(decimal(row.get(k)) for k in ('demand', 'demand_value', 'ordered', 'ordered_value'))
        supplier[warehouse, vendor].add(row, values)
        owner = owners.get(category)
        if owner:
            buyer[warehouse, owner].add(row, values)
        elif all(v is not None for v in values[::2]) and any(v > 0 for v in values[::2]):
            unmapped.append((warehouse, row['order'], row['sku'], category, *values))
    return {'supplier': supplier, 'buyer': buyer, 'unmapped': unmapped}


def aggregate(inputs, emit_detail, cancel=None):
    stores = inputs['stores']
    catalogs = inputs['catalogs']
    missing_store_codes = set(stores)
    counts = {code: Counts() for code in stores}
    cuts = {key: defaultdict(Cut) for key in ('category', 'buyer_category', 'supplier')}
    stockouts = stockout_sets(catalogs)
    details = 0
    discrepancies = Counter()
    for row in inputs['sales']:
        checkpoint(cancel)
        code, sku = row['store'], row['sku']
        if code not in stores:
            continue
        missing_store_codes.discard(code)
        if row['status'] != '正常':
            continue
        store, count = stores[code], counts[code]
        month, stock, inbound = (decimal(row[k]) for k in ('month', 'stock', 'inbound'))
        count.rows += 1
        if month is None or month < 0:
            raise PendingInputs([f'{store["name"]} / {sku} 月销待核（{row["source"]}）'])
        count.sales += month
        if not month:
            continue
        if stock is None:
            raise PendingInputs([f'{store["name"]} / {sku} 可用库存待核（{row["source"]}）'])
        origin, warehouse, product = classify(store, sku, catalogs)
        blocked = (row.get('store_name') or store['name'], sku) in inputs['blacklist']
        eligible = product is not None and not blocked
        local = origin is None and not blocked
        shortage = stock < 1
        warehouse_shortage = (sku in stockouts['山东仓'] or
                              origin == '主仓（河北）' and sku in stockouts['河北仓'])
        count.all_active += 1
        count.all_shortage += shortage
        if eligible:
            count.active += 1
            count.shortage += shortage
            count.warehouse_shortage += shortage and warehouse_shortage
            if shortage:
                count.inbound += inbound is not None and inbound > 0
                count.inbound_unknown += inbound is None or inbound < 0
            category = product['category'] or '(未分类)'
            supplier = product['supplier'] or '(未填报)'
            owner = inputs['owners'].get(category) or '(未填报)'
            for cut, key in (('category', (warehouse, category)), ('buyer_category', (owner, category)),
                             ('supplier', (warehouse, supplier))):
                cuts[cut][key].row(code, sku, shortage, shortage and warehouse_shortage)
        if local:
            count.local_active += 1
            count.local_shortage += stock <= 0
        if stock <= 0 and not blocked:
            yes = lambda v: '是' if v else '否'
            emit_detail((product['category'] if product else None, product['supplier'] if product else None,
                         code, store['name'], store['tag'], sku, row['name'], row['status'], month, stock,
                         inbound, origin, yes(local), '否', yes(eligible), yes(warehouse_shortage),
                         yes(warehouse_shortage)))
            details += 1
        if shortage and (stock > 0 or blocked):
            discrepancies[(code, '地采黑名单' if blocked else '可用库存大于0且小于1')] += 1
    if missing_store_codes:
        raise PendingInputs(['缺门店底稿：' + '、'.join(sorted(missing_store_codes))])
    order = sorted(stores, key=lambda code: (WAREHOUSES.index(stores[code]['primary']), -counts[code].active))
    return {'stores': order, 'counts': counts, 'cuts': cuts, 'details': details,
            'discrepancies': discrepancies, 'stockout_sizes': {w: len(s) for w, s in stockouts.items()}}
