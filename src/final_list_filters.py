"""Read-only final-list filters; missing evidence never becomes zero."""
from decimal import Decimal
import result_semantics as sem
import preissue

FIELDS = {'week':'周销', 'month':'月销', 'available':'可用库存', 'inbound':'在途库存', 'held':'拟签发后持有量'}
OPS = {'any':'不限', 'ge':'大于等于', 'gt':'大于', 'le':'小于等于', 'lt':'小于', 'eq':'等于', 'between':'区间（含端点）', 'missing':'资料缺失'}


def category(row, model=False):
    value = row.get('U' if model else '_category')
    return str(value).strip() if value is not None and str(value).strip() else None


def value(row, key, quantity):
    if key != 'held':
        return row.get({'week':'I', 'month':'J', 'available':'K', 'inbound':'L'}[key])
    if preissue.blocked(row) or not sem.numeric(quantity): return None
    return preissue.scalar(preissue.holding(row) + preissue.decimal(quantity))


def validate(spec):
    for key, rule in spec.get('numbers', {}).items():
        if key not in FIELDS or rule.get('op') not in OPS: raise ValueError('未知筛选条件')
        op = rule['op']
        if op in ('any', 'missing'): continue
        low = preissue.decimal(rule.get('low'), FIELDS[key]+'筛选值')
        if op == 'between' and low > preissue.decimal(rule.get('high'), FIELDS[key]+'上限'):
            raise ValueError(FIELDS[key]+'区间下限不能大于上限')
    return spec


def accepts(row, quantity, spec):
    for key, model in [('categories', False), ('profiles', True)]:
        selected = spec.get(key, [])
        if selected and category(row, model) not in selected: return False
    for key, rule in spec.get('numbers', {}).items():
        op = rule['op']
        if op == 'any': continue
        v = value(row, key, quantity)
        if op == 'missing':
            if sem.numeric(v): return False
            continue
        if not sem.numeric(v): return False
        v, low = Decimal(str(v)), Decimal(str(rule['low']))
        if not {'ge':lambda:v>=low, 'gt':lambda:v>low, 'le':lambda:v<=low, 'lt':lambda:v<low,
                'eq':lambda:v==low, 'between':lambda:low<=v<=Decimal(str(rule['high']))}[op](): return False
    return True


def description(spec):
    parts=[]
    for key, title in [('categories','商品类目'), ('profiles','模型分类')]:
        if spec.get(key): parts.append(title+'：'+'、'.join('资料缺失' if x is None else x for x in spec[key]))
    for key, r in spec.get('numbers', {}).items():
        if r['op'] == 'any': continue
        text = FIELDS[key]+OPS[r['op']]
        if r['op'] != 'missing': text += str(r['low']) + ('～'+str(r['high']) if r['op']=='between' else '')
        parts.append(text)
    return '；'.join(parts) or '类目、销量、库存：不限'


def adjusted_quantity(row, current, mode, amount):
    step = preissue.decimal(row['BD'], '箱规')
    n = preissue.decimal(amount)
    if n < 0: raise ValueError('调整数不能为负数')
    if mode in ('add','subtract','packs') and n != n.to_integral_value(): raise ValueError('最小出库单位个数须为非负整数')
    if mode == 'add': q = preissue.decimal(current) + n*step
    elif mode == 'subtract': q = max(Decimal(0),preissue.decimal(current)-n*step)
    elif mode == 'packs': q = n*step
    elif mode == 'quantity': q = n
    elif mode == 'zero': q = Decimal(0)
    else: raise ValueError('未知批量调整方式')
    return preissue.quantity(q, step)
