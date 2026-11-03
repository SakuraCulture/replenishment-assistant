"""Ordered, inventory-unit execution allocation. No model, Qt, storage or I/O."""
from decimal import Decimal, InvalidOperation

RULE = 'jst-stock-v1'


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


def allocate(rows, items, balances):
    """Mutate this run's balance only; preserve return row order and raw sources."""
    output = []
    for row in rows:
        sku = row['sku']; demand = number(row['base_quantity'], '回传数量')
        if not demand:
            continue
        item = items.get(sku)
        out = {'sku': sku, 'name': row.get('name') or (item or {}).get('商品名称') or '',
               'demand': text(demand), 'quantity': '0', 'amount': None, 'price': None,
               'step': None, 'stock': None, 'before': None, 'after': None,
               'missing': text(demand), 'reason': '', 'issue': False,
               'return_sources': row.get('sources', []), 'catalog_row': (item or {}).get('_row')}
        try:
            if row.get('_admission_error'):raise ValueError(row['_admission_error'])
            if item is None:
                raise ValueError('该仓资料无商品条目')
            state = str(item.get('商品状态') or '').strip()
            if state in ('禁用', '停用', '已禁用', '停采'):
                raise ValueError('该仓商品禁用')
            if state not in ('启用', '正常', '在用', '已启用'):
                raise ValueError('商品状态待核：' + state)
            raw_step = item.get('出库箱规')
            step = number(1 if raw_step is None or str(raw_step).strip() == '' else raw_step, '箱规')
            if step < 1 or step != step.to_integral_value():
                raise ValueError('箱规无效')
            price = number(item.get('基本售价'), '基本售价')
            stock = number(item.get('可用数'), '可用库存')
            before = balances.setdefault(sku, stock)
            out.update(step=text(step), price=text(price), stock=text(stock), before=text(before), after=text(before), amount='0')
            if demand % step:
                raise ValueError('回传数量不符合箱规倍数')
            if before < 0:
                raise ValueError('库存预留超过可用数')
            qty = (min(demand, before) // step) * step
            balances[sku] = before - qty
            reason = ('无可用库存' if before == 0 else '库存不足一箱' if qty == 0 and step > 1
                      else '库存不足' if qty < demand else '按回传分配')
            out.update(quantity=text(qty), amount=text(qty * price), after=text(before-qty), missing=text(demand-qty), reason=reason)
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
