"""Explicit AX-only price rounding; payment snapshots remain immutable."""
from decimal import Decimal, ROUND_HALF_UP, localcontext

POLICY = 'unit_price_half_up_2_v1'
HEADERS = ['商品编码', '商品名称', '付款基础数量', '原付款金额（元）', '采购单价（元）', '采购金额（元）', '舍入差额（元）']


def priced_row(quantity, amount):
    q, a = Decimal(quantity), Decimal(amount)
    if not q.is_finite() or q <= 0 or not a.is_finite() or a < 0:
        raise ValueError('采购计价需要正数基础数量及非负付款金额')
    # Retain enough precision even for tiny fractional quantities and rounding ties.
    with localcontext() as ctx:
        ctx.prec = max(100, len(q.as_tuple().digits) + len(a.as_tuple().digits) + abs(q.adjusted()) + abs(a.adjusted()) + 20)
        # Round the rational a/q directly in integer space, avoiding division
        # precision accidentally turning a value just below a tie into a tie.
        an, ad = a.as_integer_ratio()
        qn, qd = q.as_integer_ratio()
        cents, remainder = divmod(an * qd * 100, ad * qn)
        cents += 2 * remainder >= ad * qn
        price = Decimal(cents).scaleb(-2)
        procurement = (price * q).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        return {'unit_price': format(price, '.2f'), 'payment_amount': format(a, 'f'),
                'amount': format(procurement, '.2f'), 'rounding_delta': format(procurement - a, 'f')}


def preview_rows(rows):
    return [[r['sku'], r['name'], r['quantity'], r.get('payment_amount', r.get('paymentAmount', r['amount'])),
             r.get('unit_price', r.get('unitPrice', '—')), r['amount'], r.get('rounding_delta', r.get('roundingDelta', '0'))] for r in rows]


def intent_summary(p):
    return (f"原付款金额 {p.get('paymentTotalAmount', p['totalAmount'])} 元；采购金额 {p['totalAmount']} 元；"
            f"舍入差额 {p.get('roundingDelta', '0')} 元")
