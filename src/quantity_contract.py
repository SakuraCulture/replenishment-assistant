"""Strict outbound-step contract shared by postchecks and budget allocation.

Round to the nearest pack BEFORE measuring error, never floor a binary tail.
Both absolute quantity and pack-ratio errors must satisfy existing 1e-8 checks.
This does not accept a 1e-6 discrepancy or guess an invalid quantity.
"""
from decimal import Decimal,ROUND_HALF_UP

def packs(quantity,step):
    q,s=Decimal(str(quantity)),Decimal(str(step))
    if not q.is_finite() or not s.is_finite() or q<0 or s<=0:raise ValueError('补货量或最小出库单位无效')
    count=(q/s).to_integral_value(rounding=ROUND_HALF_UP)
    if abs(q-count*s)>Decimal('1e-8') or abs(q/s-count)>Decimal('1e-8'):
        raise ValueError('建议量不符合最小出库单位；超出浮点尾差容限')
    return int(count)
