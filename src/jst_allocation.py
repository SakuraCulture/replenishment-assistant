"""Ordered, inventory-unit execution allocation. No model, Qt, storage or I/O."""
from decimal import Decimal, InvalidOperation, ROUND_CEILING

RULE = 'jst-stock-v3'
MATERIAL_RULE = 'material-stock-v1'
ORDERABLE_STATES = ('启用', '正常', '在用', '已启用', '备用')


def number(value, label):
    if value is None or isinstance(value, bool) or str(value).strip() == '':
        raise ValueError(label + '待核')
    try:
        result = Decimal(str(value).strip())
    except InvalidOperation:
        raise ValueError(label + '无效') from None
    if not result.is_finite() or result < 0:
        raise ValueError(label + '无效')
    return result


def text(value):
    return format(value, 'f')


def status_issue(item):
    state = str(item.get('商品状态') or '').strip()
    if state in ('禁用', '停用', '已禁用', '停采'):
        return '该仓商品禁用'
    if state not in ORDERABLE_STATES:
        return '商品状态待核：' + state
    return ''


def allocate(rows, items, balances, *, material=False, exact=False):
    """Mutate this run's balance only; preserve return row order and raw sources."""
    output = []
    for row in rows:
        sku = row['sku']; demand = number(row['base_quantity'], '物料需求量' if material else '回传数量')
        if not demand:
            continue
        item = items.get(sku)
        out = {'sku': sku, 'name': row.get('name') or (item or {}).get('商品名称') or '',
               'demand': text(demand), 'quantity': '0', 'amount': None, 'price': None,
               'step': None, 'stock': None, 'before': None, 'after': None,
               'missing': text(demand), 'reason': '', 'issue': False,
               'carton_demand': None, 'carton_remainder': None, 'carton_increment': None,
               'return_sources': row.get('sources', []), 'catalog_row': (item or {}).get('_row')}
        try:
            if row.get('_admission_error'):raise ValueError(row['_admission_error'])
            if item is None:
                raise ValueError('该仓资料无商品条目')
            if material:
                from material_requests import eligibility
                issue = eligibility(item)
            else:
                issue = status_issue(item)
            if issue:
                raise ValueError(issue)
            raw_step = item.get('出库箱规')
            step = number(1 if raw_step is None or str(raw_step).strip() == '' else raw_step, '箱规')
            if step < 1 or step != step.to_integral_value():
                raise ValueError('箱规无效')
            price = number(item.get('基本售价'), '基本售价')
            stock = number(item.get('可用数'), '可用库存')
            before = balances.setdefault(sku, stock)
            out.update(step=text(step), price=text(price), stock=text(stock), before=text(before), after=text(before), amount='0')
            carton_demand = demand if material or exact else (demand / step).to_integral_value(rounding=ROUND_CEILING) * step
            increment = carton_demand - demand
            # Keep the original tail's meaning in saved rows; record the upward
            # padding separately instead of turning that tail into a negative.
            remainder = demand % step
            out.update(carton_demand=text(carton_demand), carton_remainder=text(remainder), carton_increment=text(increment))
            if before < 0:
                raise ValueError('库存预留超过可用数')
            # Normalize the demand once. A warehouse shortage allocates every
            # remaining inventory unit, including a partial carton.
            qty = min(carton_demand, before)
            balances[sku] = before - qty
            reason = ('无可用库存' if before == 0
                      else '库存不足，按剩余可用库存分配（含不足整箱）' if qty < carton_demand else '按回传分配')
            if material and qty == demand:
                reason = '按物料核对需求分配'
            if exact and qty == demand:
                reason = '按手动更正数量分配'
            if increment:
                reason += '；回传按箱规向上规整 '+text(demand)+'→'+text(carton_demand)+'，补齐'+text(increment)
            out.update(quantity=text(qty), amount=text(qty * price), after=text(before-qty), missing=text(max(demand-qty, Decimal(0))), reason=reason)
        except ValueError as exc:
            out.update(issue=True, reason=str(exc))
        output.append(out)
    return output


def totals(rows):
    return {'quantity': text(sum((Decimal(r['quantity']) for r in rows), Decimal(0))),
            'amount': text(sum((Decimal(r['amount']) for r in rows if r['amount'] is not None), Decimal(0))),
            'positive': sum(Decimal(r['quantity']) > 0 for r in rows),
            'shortages': sum(Decimal(r['missing']) > 0 for r in rows),
            'issues': sum(r['issue'] for r in rows)}
