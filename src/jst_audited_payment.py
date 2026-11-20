"""Check complete ordinary-audit merchandise evidence before preparing payment."""
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256

from jst_allocation import number
from jst_auto_order_template import decimal_text
from payment_evidence import HEAD_KEYS
from runtime_support import checkpoint
from snapshot_json import dumps


KIND = 'jst_audited_order_v1'
LABEL = '聚水潭审核后订单明细'
AUDITED = '已客审待财审'
CENT = Decimal('0.01')
CURRENCIES = {'CNY', 'RMB', '人民币', '￥', '¥'}


def money(value, label):
    value = number(value, label)
    if value < 0 or value != value.quantize(CENT, rounding=ROUND_HALF_UP):
        raise ValueError(label + '不是有效的分币金额')
    return value


def prepare(order, receipt, cancel=None):
    payload = order['payload']
    binding = (order['proof'] or {}).get('binding')
    if (receipt.get('found') is not True or receipt.get('contact_ok') is not True
            or receipt.get('native_status') != AUDITED or receipt.get('binding') != binding
            or receipt.get('online') != order['online'] or receipt.get('internal') != order['internal']):
        raise ValueError('审核后明细的仓、店铺、买家或原单身份尚未核实')
    if receipt.get('currency') not in CURRENCIES:
        raise ValueError('聚水潭成交币种不是已核验的人民币')
    expected = {sku: (qty, price) for sku, qty, price in payload['lines']}
    items = receipt.get('items')
    if not isinstance(items, list) or not expected or receipt.get('detail_count') != len(items) or len(items) != len(expected):
        raise ValueError('审核后整单商品款数与原仓单不同，缺行须核对')
    verified = {}
    for item in items:
        checkpoint(cancel)
        sku = item.get('sku')
        if sku not in expected or sku in verified:
            raise ValueError('审核后明细存在重复编码或原仓单外商品')
        if item.get('item_status') not in (None, '', 'Normal') or item.get('refund_status') not in (None, '', 'NoRefund'):
            raise ValueError(f'商品 {sku} 的成交或退款状态待核')
        # The original SKU's native quantity is the submitted inventory unit.
        # An explicit alternate sales unit requires an evidenced 1-to-1 factor.
        factor = item.get('per_unit')
        unit = item.get('sales_unit')
        if ((factor is not None and number(factor, f'{sku} 单位换算') != 1)
                or (unit and factor is None)):
            raise ValueError(f'商品 {sku} 存在待核的单位换算')
        qty = number(item.get('qty'), f'{sku} 审核后数量')
        price = number(item.get('price'), f'{sku} 成交单价')
        amount = money(item.get('amount'), f'{sku} 商品金额')
        if min(qty, price) < 0 or (qty * price).quantize(CENT, rounding=ROUND_HALF_UP) != amount:
            raise ValueError(f'商品 {sku} 的数量、成交价和行金额不符，行级优惠或价差待核')
        verified[sku] = (qty, price, amount)
    merchandise = sum((row[2] for row in verified.values()), Decimal(0))
    freight = money(receipt.get('freight'), '原单运费')
    total = money(receipt.get('amount'), '原单应付金额')
    difference = total - merchandise - freight
    if difference:
        raise ValueError(f'应付 {total:.2f} 元，商品 {merchandise:.2f} 元，运费 {freight:.2f} 元，'
                         f'优惠或其他金额差异 {difference:+.2f} 元待核')
    headers = ['商品编码', '审核后数量', '成交单价', '商品金额', '币种', '数量单位']
    rows = []
    for line, (sku, (ordered, original_price)) in enumerate(expected.items(), 1):
        qty, price, amount = verified[sku]
        values = [sku, decimal_text(qty), decimal_text(price), f'{amount:.2f}', 'CNY', '库存单位']
        rows.append({'sku': sku, 'name': '', 'ordered': ordered, 'quantity': values[1],
                     'price': values[2], 'amount': values[3], 'currency': 'CNY',
                     'allocated_amount': decimal_text(Decimal(ordered) * Decimal(original_price)),
                     'sheet': LABEL, 'row': line, 'headers': headers, 'values': values})
    totals = {'goods': f'{merchandise:.2f}', 'freight': f'{freight:.2f}',
              'payable': f'{total:.2f}', 'unexplained': f'{difference:.2f}'}
    # Keep evidence stable across repeat reads and changes in the platform's row order.
    raw = dumps({'kind': KIND, 'binding': binding, 'online': order['online'], 'internal': order['internal'],
                 'status': AUDITED, 'totals': totals, 'headers': headers,
                 'rows': [row['values'] for row in rows]}).encode('utf-8')
    candidate = {
        'head': {key: payload['head'][key] for key in HEAD_KEYS}, 'order_digest': order['digest'],
        'gid': order['gid'], 'online': order['online'], 'internal': order['internal'],
        'binding': binding, 'source_kind': KIND, 'native_status': AUDITED,
        'file': {'sha': sha256(raw).hexdigest(), 'name': f"{LABEL}_{order['internal']}.json"},
        'rows': rows, 'quantity': decimal_text(sum((row[0] for row in verified.values()), Decimal(0))),
        'amount': totals['goods'], 'totals': totals,
    }
    return candidate, raw
