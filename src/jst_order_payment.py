"""Validate complete native order detail before preparing a payment candidate."""
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256

from jst_allocation import number
from jst_auto_order_template import decimal_text
from jst_order_confirmation import PAYMENT_STATES, buyer_issue
from payment_evidence import HEAD_KEYS
from runtime_support import checkpoint
from snapshot_json import dumps


KIND = 'jst_order_detail_v1'
LABEL = '聚水潭原订单明细'
LEGACY_KIND = 'jst_audited_order_v1'
LEGACY_LABEL = '聚水潭审核后订单明细'
NATIVE_KINDS = frozenset((KIND, LEGACY_KIND))
CENT = Decimal('0.01')
CURRENCIES = {'CNY', 'RMB', '人民币', '￥', '¥'}


def money(value, label):
    value = number(value, label)
    if value < 0 or value != value.quantize(CENT, rounding=ROUND_HALF_UP):
        raise ValueError(label + '不是有效的分币金额')
    return value


def verify_details(order, receipt, cancel=None):
    """Check payment facts after independently corroborating a hidden buyer's original order."""
    binding = (order['proof'] or {}).get('binding')
    if (receipt.get('found') is not True or not binding
            or receipt.get('native_status') not in PAYMENT_STATES or receipt.get('binding') != binding
            or receipt.get('online') != order['online'] or receipt.get('internal') != order['internal']):
        raise ValueError('聚水潭原单的企业、仓、店铺或单号尚未核实')
    buyer_problem = buyer_issue(receipt)
    if buyer_problem and receipt.get('buyer_check') != 'unavailable':
        raise ValueError(buyer_problem)
    expected, items = (verify_import_items(order, receipt, cancel) if buyer_problem else
                       _read_items(order['payload'], receipt, cancel))
    verified, totals = _verified_amounts(items, receipt, cancel)
    if buyer_problem:
        _verify_frozen_prices(order, expected, verified, totals, cancel)
    return expected, verified, totals


def verify_import_items(order, receipt, cancel=None):
    """Corroborate the uniquely located order using its frozen import and complete quantities."""
    try:
        proof = order['proof']
        if not order.get('file_sha') or proof.get('file_sha') != order['file_sha'] or proof.get('amount') is None:
            raise ValueError('缺少原导入文件或平台预览凭据')
        expected, items = _read_items(order['payload'], receipt, cancel)
        for sku, (quantity, _) in expected.items():
            checkpoint(cancel)
            actual = items[sku][0]
            if actual != Decimal(quantity):
                raise ValueError(f'商品 {sku} 的数量与原生成单不同（原单 {quantity}，平台 {decimal_text(actual)}）')
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise ValueError(f'{buyer_issue(receipt)} · {exc}') from None
    return expected, items


def _verify_frozen_prices(order, expected, verified, totals, cancel):
    for sku, (_, price) in expected.items():
        checkpoint(cancel)
        if verified[sku][1] != Decimal(price):
            raise ValueError(f'商品 {sku} 的成交单价与原生成单不同（原单 {price}，平台 {verified[sku][1]}），付款待核')
    preview = money(order['proof']['amount'], '原平台预览金额')
    if Decimal(totals['payable']) != preview:
        raise ValueError(f"平台应付 {totals['payable']} 元与原导入预览 {preview:.2f} 元不同，付款待核")


def _read_items(payload, receipt, cancel):
    if receipt.get('detail_error'):
        raise ValueError(receipt['detail_error'])
    expected = {sku: (qty, price) for sku, qty, price in payload['lines']}
    items = receipt.get('items')
    if not isinstance(items, list) or not expected or receipt.get('detail_count') != len(items) or len(items) != len(expected):
        raise ValueError('平台整单商品款数与原仓单不同，缺行须核对')
    checked = {}
    for item in items:
        checkpoint(cancel)
        if not isinstance(item, dict):
            raise ValueError('平台整单商品明细格式待核')
        sku = item.get('sku')
        if not isinstance(sku, str) or sku not in expected or sku in checked:
            raise ValueError('平台明细存在重复编码或原仓单外商品')
        if item.get('item_status') not in (None, '', 'Normal') or item.get('refund_status') not in (None, '', 'NoRefund'):
            raise ValueError(f'商品 {sku} 的成交或退款状态待核')
        # The original SKU's native quantity is the submitted inventory unit.
        # An explicit alternate sales unit requires an evidenced 1-to-1 factor.
        factor = item.get('per_unit')
        unit = item.get('sales_unit')
        if ((factor is not None and number(factor, f'{sku} 单位换算') != 1)
                or (unit and factor is None)):
            raise ValueError(f'商品 {sku} 存在待核的单位换算')
        qty = number(item.get('qty'), f'{sku} 平台数量')
        checked[sku] = (qty, item)
    return expected, checked


def _verified_amounts(items, receipt, cancel):
    if receipt.get('currency') not in CURRENCIES:
        raise ValueError('聚水潭成交币种不是已核验的人民币')
    verified = {}
    for sku, (qty, item) in items.items():
        checkpoint(cancel)
        price = number(item.get('price'), f'{sku} 成交单价')
        amount = money(item.get('amount'), f'{sku} 商品金额')
        calculated = (qty * price).quantize(CENT, rounding=ROUND_HALF_UP)
        if calculated != amount:
            raise ValueError(f'商品 {sku} 按数量 {decimal_text(qty)} × 成交单价 {decimal_text(price)} '
                             f'为 {calculated:.2f} 元，平台行金额 {amount:.2f} 元，差额 {amount-calculated:+.2f} 元，付款待核')
        verified[sku] = (qty, price, amount)
    merchandise = sum((row[2] for row in verified.values()), Decimal(0))
    freight = money(receipt.get('freight'), '原单运费')
    total = money(receipt.get('amount'), '原单应付金额')
    difference = total - merchandise - freight
    if difference:
        raise ValueError(f'应付 {total:.2f} 元，商品 {merchandise:.2f} 元，运费 {freight:.2f} 元，'
                         f'优惠或其他金额差异 {difference:+.2f} 元待核')
    totals = {'goods': f'{merchandise:.2f}', 'freight': f'{freight:.2f}',
              'payable': f'{total:.2f}', 'unexplained': f'{difference:.2f}'}
    return verified, totals


def prepare(order, receipt, cancel=None):
    expected, verified, totals = verify_details(order, receipt, cancel)
    payload = order['payload']
    binding = order['proof']['binding']
    headers = ['商品编码', '平台数量', '成交单价', '商品金额', '币种', '数量单位']
    rows = []
    for line, (sku, (ordered, original_price)) in enumerate(expected.items(), 1):
        qty, price, amount = verified[sku]
        values = [sku, decimal_text(qty), decimal_text(price), f'{amount:.2f}', 'CNY', '库存单位']
        rows.append({'sku': sku, 'name': '', 'ordered': ordered, 'quantity': values[1],
                     'price': values[2], 'amount': values[3], 'currency': 'CNY',
                     'allocated_amount': decimal_text(Decimal(ordered) * Decimal(original_price)),
                     'sheet': LABEL, 'row': line, 'headers': headers, 'values': values})
    # Keep evidence stable across repeat reads and changes in the platform's row order.
    raw = dumps({'kind': KIND, 'binding': binding, 'online': order['online'], 'internal': order['internal'],
                 'totals': totals, 'headers': headers,
                 'rows': [row['values'] for row in rows]}).encode('utf-8')
    candidate = {
        'head': {key: payload['head'][key] for key in HEAD_KEYS}, 'order_digest': order['digest'],
        'gid': order['gid'], 'online': order['online'], 'internal': order['internal'],
        'binding': binding, 'source_kind': KIND,
        'file': {'sha': sha256(raw).hexdigest(), 'name': f"{LABEL}_{order['internal']}.json"},
        'rows': rows, 'quantity': decimal_text(sum((row[0] for row in verified.values()), Decimal(0))),
        'amount': totals['goods'], 'totals': totals,
    }
    return candidate, raw


def existing_payment(candidate, payment, *, original_internal=None):
    """Compare merchandise facts while retaining the official version and its source."""
    if not payment or payment['payload'].get('pending'):
        return None

    def facts(rows, quantity_key):
        values = {}
        for row in rows:
            supplied = row.get(quantity_key)
            qty = Decimal(str(supplied)) if supplied is not None else None
            amount = row.get('amount')
            amount = Decimal(str(amount)) if amount is not None else None
            if qty or amount or qty is None:
                currency = row.get('currency')
                values[row['sku']] = (qty, amount, 'CNY' if currency in CURRENCIES else currency)
        return values

    saved = facts(payment['payload']['rows'], 'base_quantity')
    incoming = facts(candidate['rows'], 'quantity')
    changed = sum(saved.get(sku) != incoming.get(sku) for sku in saved.keys() | incoming.keys())
    refs = {str(r['source_order']['内部订单号']) for r in payment['payload'].get('raw_rows', [])
            if (r.get('source_order') or {}).get('内部订单号')}
    if refs and refs != {candidate['internal']} and refs != {original_internal}:
        raise ValueError('已保存付款单登记了其他聚水潭原单，须先核对关联')
    amounts = [amount for _, amount, _ in saved.values()]
    quantities = [qty for qty, _, _ in saved.values()]
    same_source = any(file.get('sha') == candidate['file']['sha'] for file in payment['payload'].get('files', ()))
    freight_to_check = not same_source and bool(Decimal(candidate['totals']['freight']))
    return {'version': payment['version'], 'changed_skus': changed, 'freight_to_check': freight_to_check,
            'quantity': decimal_text(sum(quantities, Decimal(0))) if all(q is not None for q in quantities) else None,
            'amount': f'{sum(amounts, Decimal(0)):.2f}' if all(a is not None for a in amounts) else None}


def same_order_facts(old, new):
    """Keep an earlier candidate if the live check returns the same business facts."""
    keys = ('head','order_digest','gid','online','internal','binding','totals')
    if any(old.get(key) != new.get(key) for key in keys):
        return False
    columns = ('sku','quantity','price','amount','currency','ordered','allocated_amount')
    return [tuple(row[k] for k in columns) for row in old['rows']] == [tuple(row[k] for k in columns) for row in new['rows']]
