"""Read-only coverage projection over frozen funnel facts. No model/Qt/DB imports."""
from collections import OrderedDict, Counter
from decimal import Decimal, InvalidOperation
from runtime_support import checkpoint

VERSION = 'coverage-1'
STAGES = ('model', 'signed', 'return', 'payment', 'receipt')
NAMES = dict(zip(STAGES, ('模型建议', '签发', '门店确认', '实际下单', '实际收货')))
GROUPS = ('必采', '非必采', '属性待核')
DEFINITIONS = {
    'forecast': '制单时预测日均×当时趋势修正；五段固定同一依据，不用最新销量反算历史。',
    'increment': '本环节库存单位数量÷当时预测日均；实收新增天数不是门店当前库存天数。',
    'stock': '现货=可用÷日均；含在途=(可用+在途)÷日均；签发后=(可用+在途+签发)÷日均，均为制单时静态预计。',
    'weighted': 'Σ(本环节数量×签发价)÷Σ(预测日均×签发价)，仅代表本次商品，非全店实际周转。',
    'missing': '有效数量0为0天；未取得、无销量参考、缺价、异常或缺历史依据不能当0。',
    'cohort': '比较范围内使用共同有效订单及固定商品集合，保留数量0；未选择比较的后续阶段不展示比较天数。',
    'rounds': '跨多轮天数仅展示最新业务日并标日期；逐轮另列，不累计历史库存。',
    'warehouse': '同日同店同SKU跨仓只有单位、预测、价格及库存依据一致才合并，库存和日均各计一次；不一致停止汇总。',
}


def number(value):
    if value is None or isinstance(value, bool) or value == '': return None
    try: n = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError): return None
    return n if n.is_finite() else None


def item(row):
    o = row.get('observations', {})
    base, trend, k, inbound, depth, lead = (number(o.get(x)) for x in ('AN','AM','K','L','AG','AH'))
    d = base * trend if base is not None and trend is not None else None
    reasons = []
    if row.get('scope') != '纳入': reasons.append('未纳入分析：' + '；'.join(row.get('reasons', []) or [row.get('scope', '待核')]))
    if not row.get('unit'): reasons.append('库存单位未留存')
    if d is None: reasons.append('历史预测依据不足或无效')
    elif base < 0 or trend < 0 or d <= 0: reasons.append('无有效销量参考' if d == 0 else '预测日均异常')
    valid = not reasons
    price = number(row.get('price'))
    price_reason = '' if price is not None and price > 0 else '签发价缺失或非正数，不参与货值加权'
    quantities = {s: number(row.get('quantities', {}).get(s)) for s in STAGES}
    stage_reasons = {s: list(reasons) for s in STAGES}
    days = {}
    for s in STAGES:
        if not row.get('valid', {}).get(s) or quantities[s] is None: stage_reasons[s].append(NAMES[s]+'资料未取得或待核')
        elif quantities[s] < 0: stage_reasons[s].append(NAMES[s]+'数量异常')
        days[s] = quantities[s] / d if not stage_reasons[s] else None
    stock_reasons = list(reasons)
    if k is None or k < 0: stock_reasons.append('可用库存缺失或为负数')
    cash = k / d if not stock_reasons else None
    position_reasons = list(stock_reasons)
    if inbound is None or inbound < 0: position_reasons.append('在途库存缺失或为负数')
    position = (k+inbound)/d if not position_reasons else None
    signed = position+days['signed'] if position is not None and days['signed'] is not None else None
    period = depth+lead if depth is not None and depth >= 0 and lead is not None and lead >= 0 else None
    risk = []
    if cash is not None and lead is not None and lead >= 0 and cash < lead:
        risk.append('现货覆盖短于到货周期；有在途须核对到店时间' if inbound is not None and inbound > 0 else '现货覆盖短于到货周期，需核对缺货风险')
    if signed is not None and period is not None and signed > period: risk.append('签发后覆盖高于当时保护期，需结合箱规及铺货原因核对')
    return {'day':row.get('day'), 'store':row.get('store'), 'warehouse':row.get('warehouse'),
            'order':row.get('order'), 'sku':row.get('sku'), 'name':row.get('name'), 'unit':row.get('unit'),
            'forecast_daily':d, 'available':k, 'inbound':inbound, 'price':price,
            'stage_days':days, 'stage_reasons':stage_reasons, 'base_reasons':reasons, 'price_reason':price_reason,
            'available_days':cash, 'position_days':position, 'signed_position_days':signed,
            'protection_days':period, 'lead_days':lead, 'stock_reasons':stock_reasons,
            'position_reasons':position_reasons, 'risks':risk,
            'signed_to_receipt_loss_days':days['signed']-days['receipt'] if days['signed'] is not None and days['receipt'] is not None else None}


def summarize(rows, through=None, cancel=None, _prepared=None):
    """Latest round only; optional common completed-order cohort through a stage.

    None reports each stage only if all base rows are known, never a partial total.
    A through stage selects a shared completed-order cohort for a comparison.
    """
    if through is not None and through not in STAGES: raise ValueError('未知天数比较环节')
    latest = max((r['day'] for r in rows), default=None)
    current = [r for r in rows if r['day'] == latest]
    excluded = Counter(); pairs = []; seen = set()
    for row in current:
        checkpoint(cancel)
        key = (row['order'], row['sku'])
        if key in seen: raise ValueError('分析记录存在重复订单商品，不能重复累计')
        seen.add(key)
        detail = _prepared[id(row)] if _prepared is not None else item(row)
        if detail['base_reasons'] or detail['price_reason']:
            excluded.update(detail['base_reasons'] + ([detail['price_reason']] if detail['price_reason'] else []))
        else: pairs.append((row,detail))
    available_orders = list(dict.fromkeys(r['order'] for r,d in pairs))
    compared = STAGES[:STAGES.index(through)+1] if through else STAGES
    if through:
        bad_orders = {r['order'] for r,d in pairs if any(d['stage_days'][s] is None for s in compared)}
        pairs = [(r,d) for r,d in pairs if r['order'] not in bad_orders]
    else: bad_orders = set()
    cohorts = OrderedDict()
    for row,detail in pairs: cohorts.setdefault((row['store'],row['sku']),[]).append((row,detail))
    conflicts = []
    for key, members in cohorts.items():
        first = members[0][1]
        if len(members)>1 and (first['available'] is None or first['inbound'] is None or any(any(d[k]!=first[k] for k in ('unit','forecast_daily','price','available','inbound')) for r,d in members[1:])):
            conflicts.append({'store':key[0], 'sku':key[1], 'reason':'跨单/跨仓同商品预测、单位、价格或库存依据不一致'})
    denominator = sum((members[0][1]['forecast_daily']*members[0][1]['price'] for members in cohorts.values()),Decimal(0))
    days = {}; stage_reasons = {}
    for s in STAGES:
        why = []
        if through and s not in compared: why.append('未纳入本次比较环节')
        if not pairs: why.append('没有共同有效商品')
        if conflicts: why.append('跨单/跨仓同商品依据冲突，停止汇总')
        if any(d['stage_days'][s] is None for r,d in pairs): why.append(NAMES[s]+'存在未取得或待核数量，不能当0')
        days[s] = sum((number(r['quantities'][s])*d['price'] for r,d in pairs),Decimal(0))/denominator if not why and denominator>0 else None
        stage_reasons[s] = why
    return {'version':VERSION,'basis_day':latest,'through':through,'stage_days':days,'stage_reasons':stage_reasons,
            'input_rows':len(rows),'round_rows':len(current),'eligible_rows':len(pairs),'unique_demand_items':len(cohorts),
            'orders':len({r['order'] for r,d in pairs}), 'round_orders':len({r['order'] for r in current}),
            'excluded_order_count':len(bad_orders),'base_order_count':len(available_orders),
            'eligible_fraction':Decimal(len(pairs))/len(current) if current else None,
            'excluded_reasons':dict(excluded),'conflicts':conflicts,'daily_value':denominator if not conflicts and pairs else None,
            'note':'本次商品按签发价加权；跨多轮仅最新业务日，不是当前库存周转。'}


def groups(rows, through=None, cancel=None, _prepared=None):
    return {g:summarize([r for r in rows if r['group']==g],through,cancel,_prepared) for g in GROUPS}


def projection(rows, through='signed', cancel=None):
    """Separate extension: never mutates legacy facts, metrics or fingerprint."""
    def group_by(fields):
        result=OrderedDict()
        for r in rows: result.setdefault(tuple(r[f] for f in fields),[]).append(r)
        return result
    prepared={}
    for r in rows: checkpoint(cancel);prepared[id(r)]=item(r)
    result={'version':VERSION,'definitions':DEFINITIONS,'through':through,
            'overview':summarize(rows,through,cancel,prepared),'groups':groups(rows,through,cancel,prepared),'details':list(prepared.values())}
    for name,fields in [('orders',('store','warehouse','order')),('warehouses',('store','warehouse')),('stores',('store',)),('rounds',('store','day','warehouse'))]:
        values=[]
        for key,rs in group_by(fields).items():
            checkpoint(cancel)
            values.append({**dict(zip(fields,key)),'name':rs[0]['store_name'],'coverage':summarize(rs,cancel=cancel,_prepared=prepared),'groups':groups(rs,cancel=cancel,_prepared=prepared)})
        result[name]=values
    return result
