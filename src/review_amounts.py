"""Read-only Step 4 money projections; never stored in calculation/archive data."""
from decimal import Decimal
import new_products
import result_semantics as sem

NOTE = '人工改量与新品可能交叉，不能相加作为总额。新品口径与签发单的本月新品＋往期新品一致。'
MANUAL_HELP = '仅统计已应用的人工改量商品（含改为0）；增减额＝这些商品的最终金额－人工调整前的自动安排金额。清单补足单独保留原分类，不算直接人工改量。'
NEW_HELP = '本月新品＋往期新品的最终执行金额；未选择或预算未安排的数量为0。地采、禁用、停采不计入。'
MONEY_KEYS = ('manual_amount', 'manual_delta', 'new_amount')
COUNT_KEYS = ('manual_count', 'manual_unknown', 'delta_unknown', 'new_count', 'new_unknown')


def is_manual(d):
    return d.get('_source') == '资料匹配' and sem.work_eligible(d) and d.get('_preissue', {}).get('kind') == 'manual'


def is_new(d):
    # The union of normal_exports.groups' two new-product sheets; focus only
    # moves a row between those sheets and cannot change this union.
    return d.get('_source') == '资料匹配' and sem.work_eligible(d) and bool(d.get('_preissue', {}).get('current_month') or new_products.is_new(d))


def empty():
    return {**dict.fromkeys(MONEY_KEYS, Decimal(0)), **dict.fromkeys(COUNT_KEYS, 0)}


def summarize(rows):
    out = empty()
    for d in rows:
        manual, new = is_manual(d), is_new(d)
        if not (manual or new):continue
        amount = sem.order_amount(d)
        known = sem.numeric(amount)
        if manual:
            out['manual_count'] += 1
            if known:out['manual_amount'] += Decimal(str(amount))
            else:out['manual_unknown'] += 1
            before = d['_preissue'].get('automatic_amount')
            if known and sem.numeric(before):out['manual_delta'] += Decimal(str(amount)) - Decimal(str(before))
            else:out['delta_unknown'] += 1
        if new:
            out['new_count'] += 1
            if known:out['new_amount'] += Decimal(str(amount))
            else:out['new_unknown'] += 1
    # Match the existing store summary's cent precision; the batch total then
    # equals the sum of the amounts actually displayed for each store.
    for key in MONEY_KEYS:out[key] = out[key].quantize(Decimal('.01'))
    return out


def combine(values):
    out = empty()
    for value in values:
        for key in out:out[key] += value[key]
    return out


def amount_text(value, key):
    prefix = '已计价 ' if value[key + '_unknown'] else ''
    return prefix + f"{value[key + '_amount']:,.2f}"


def delta_text(value):
    suffix = f"（{value['delta_unknown']}款增减待核）" if value['delta_unknown'] else ''
    return f"较自动安排 {value['manual_delta']:+,.2f} 元" + suffix


def count_text(value, key):
    return f"{value[key + '_count']}款" + (f" · {value[key + '_unknown']}款金额待核" if value[key + '_unknown'] else '')


def cached(host, result):
    """Published results are replaced on apply/recompute/restore, never edited.

    Keep only current result identities, so frequent task progress updates do
    not rescan every store or retain obsolete drafts.
    """
    current = getattr(host, 'results', {})
    cache = {s:entry for s,entry in getattr(host, '_review_amounts_cache', {}).items() if current.get(s) is entry[0]}
    key = result['store']
    if key not in cache or cache[key][0] is not result:cache[key] = (result, summarize(result['rows']))
    host._review_amounts_cache = cache
    return cache[key][1]
