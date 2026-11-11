"""Default must-sell holding floor, separate from the unchanged demand forecast.

Evidence is frozen on newly calculated rows. Archived rows without this evidence
retain their original execution quantities. Stock and inbound use the existing
nonnegative holding convention; order quantities must satisfy the outbound step.
"""
from decimal import Decimal, ROUND_CEILING
from model import number
from quantity_contract import packs

VERSION = 'mandatory-holding-1'


def dec(value):return Decimal(str(value))


def apply(row, minimum):
    """Raise a validated candidate to the must-sell floor, never an exclusion."""
    import result_semantics as sem
    if not row.get('_must_sell') or sem.exclusion_reason(row):return
    audit = {'version':VERSION, 'minimum':minimum, 'state':'待核'}
    row['_mandatory_holding'] = audit
    reason = sem.issue_text(row)
    if row.get('_source') != '资料匹配':reason = '商品资料未匹配，不能补造中心仓数量'
    elif not row.get('_meta',{}).get('product_tags_known'):reason = '商品标签资料不完整，不能确认日配排除'
    if not reason:
        for key, title in [('H','日销'),('I','周销'),('J','月销'),('K','可用库存'),('L','在途库存'),
                           ('_candidate_qty','原建议量'),('BD','最小出库单位'),('V','基本售价')]:
            if not number(row.get(key)) or key in ('BD','V') and row[key] <= 0:
                reason = title+'待核';break
    if reason:audit['reason'] = reason;return
    if not number(minimum) or minimum <= 0:raise ValueError('必采最低持有基数须为有效正数')
    before = row['_candidate_qty'];step = dec(row['BD'])
    packs(before, row['BD'])
    held = dec(max(row['K'],0)) + dec(max(row['L'],0))
    deficit = max(dec(minimum)-held, Decimal(0))
    required = (deficit/step).to_integral_value(rounding=ROUND_CEILING)*step
    candidate = max(dec(before),required)
    qty = float(candidate);money = candidate*dec(row['V'])
    if not number(qty) or dec(qty) != candidate or not number(float(money)):
        raise ValueError('必采最低持有数量或金额超过安全表示范围：'+str(row.get('E')))
    packs(qty,row['BD'])
    if candidate>dec(before):row.update(_candidate_qty=qty,_candidate_amount=float(money))
    audit.update(state='已核验', holding=float(held), required_quantity=float(required),
                 candidate_before=before, added_quantity=float(candidate-dec(before)))


def gap(row, quantity):
    audit = row.get('_mandatory_holding',{})
    return bool(audit.get('state') == '已核验' and number(quantity)
                and dec(quantity) < dec(audit['required_quantity']))


def explanation(row):
    audit = row.get('_mandatory_holding',{})
    if not audit:return ''
    if audit['state'] != '已核验':return '必采最低持有待核：'+audit.get('reason','资料不足')
    return (f"必采最低持有 {audit['minimum']:g} 个库存单位；可用及在途覆盖 {audit['holding']:g}；"
            f"按出库单位补足至少 {audit['required_quantity']:g}，较原建议追加 {audit['added_quantity']:g}")
