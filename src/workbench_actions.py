"""Shared workbench decisions. No Qt, model changes or implicit persistence."""
import copy
from decimal import Decimal, InvalidOperation


def budget_value(mode, raw, profile):
    if mode == 0:
        raise ValueError('请选择预算方式')
    budget = None
    if mode == 2:
        try:
            raw = raw.strip()
            if not raw:
                raise ValueError('请填写本次预算')
            value = Decimal(raw)
            if not value.is_finite() or value < 0 or value > 1000000000 or value != value.quantize(Decimal('.01')):
                raise ValueError('预算须为0至10亿元，最多两位小数')
            budget = float(value)
        except InvalidOperation:
            raise ValueError('请输入有效预算金额') from None
    # The caller retains the original validation order for warehouse settings.
    return budget


def parameter_settings(result, config, preset, admission_snapshot):
    import parameter_schema as schema
    schema.validate(config)
    result['admission'] = admission_snapshot
    result.update(parameter_config=config, warehouses=copy.deepcopy(config['warehouses']),
                  exception=config['exception'], preset={k: v for k, v in preset.items() if k != 'config'},
                  parameter_signature=schema.signature(config))
    return result


def recalculation_impact(results, codes):
    return {
        'new_products': [s for s in codes if results.get(s, {}).get('new_products')],
        'manual': {s: results[s] for s in codes if results.get(s, {}).get('preissue')},
    }


def plan_record(plan, original, mapping, store=None):
    import parameter_maintenance as pm
    import replenishment_plans as plans
    value = copy.deepcopy(original)
    if plan.get('parameter_mode')=='per_store_presets':
        if store not in plan.get('_resolved_presets',{}):raise ValueError('补货预案尚未冻结该店参数，请重新采用预案：'+str(store))
        p=copy.deepcopy(plan['_resolved_presets'][store])
        p['plan_source']={k:copy.deepcopy(plan[k]) for k in ('id','name','revision','updated','standard_version')}
    else:
        p = plans.preset(plan)
        p['config'] = pm.plan_config(plan, original['preset']['config'])
    value['preset'] = pm.bind(p, mapping)
    return value


def record_settings(record, profile, catalogs, admission_snapshot):
    """The same budget/config/route checks used by the workbench's controls."""
    import parameter_schema as schema
    import warehouse_catalog
    p = record['preset']; c = copy.deepcopy(p['config']); ui = record['ui']
    budget = budget_value(ui['mode'], ui.get('amount', ''), profile)
    if any(d < 0 or l < 0 or d + l <= 0 for d, l in c['warehouses'].values()):
        raise ValueError('请填写未知仓的备货和到货天数')
    if budget is not None and not profile:
        raise ValueError('限额补货需要有效必卖清单')
    result = {'warehouses': c['warehouses'], 'exception': c['exception'], 'budget': budget}
    if catalogs:
        result['supply_route'] = warehouse_catalog.validate_route(
            {k: ui.get(k) for k in ('primary', 'secondary')}, catalogs)
    schema.validate(c)
    return parameter_settings(result, c, p, admission_snapshot)
