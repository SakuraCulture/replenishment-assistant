"""Durable store/warehouse parameters; daily budget persistence is explicit."""
import copy
import json
from app_defaults import WAREHOUSES
import parameter_schema as schema

def fallback_preset():
    """Internal calculation fallback; never a selectable/persisted preset."""
    return {'id':'standard','name':'自动填写参数','version':1,'builtin':True,
            'implicit_default':True,'config':schema.standard()}

def decode(value):
    value=copy.deepcopy(value)
    if value is not None and not value.get('preset'):value['preset']=json.loads(json.dumps(fallback_preset()))
    return value

def has_parameters(value):
    return bool(value and value.get('preset') and not value['preset'].get('implicit_default'))

def encode(value):
    """Keep route/budget records but do not persist the implicit fallback config."""
    value=copy.deepcopy(value)
    if value is None:return None
    p=value.get('preset')
    if p and p.get('implicit_default'):
        from parameter_presets import signature
        if signature(p['config'])==signature(schema.standard()):value.pop('preset')
        else:p.pop('implicit_default',None)
    return value

def record(preset,config,ui,old=None,*,include_budget=False,saved='',preserve_implicit=False):
    schema.validate(config)
    primary,secondary=ui.get('primary'),ui.get('secondary')
    if primary not in (None,*WAREHOUSES) or secondary not in (None,*WAREHOUSES) or secondary and (not primary or primary==secondary):raise ValueError('主从仓设置无效，请先调整')
    durable={k:ui.get(k) for k in ('primary','secondary')}
    previous=(old or {}).get('ui',{})
    for key,default in [('mode',0),('amount','')]:durable[key]=ui.get(key,default) if include_budget else previous.get(key,default)
    # Inclusion in a batch is not a store default. Old records still load compatibly.
    durable['selected']=True
    p={**copy.deepcopy(preset),'config':copy.deepcopy(config)}
    if not preserve_implicit:p.pop('implicit_default',None)
    return {'preset':p,'ui':durable,'saved':saved,
            **({'name':old['name']} if old and old.get('name') else {})}

def matches(saved,preset,config,ui):
    if not has_parameters(saved):return False
    return (saved['preset']['config']==config and
            all(saved['ui'].get(k)==ui.get(k) for k in ('primary','secondary')) and
            all(saved['preset'].get(k)==preset.get(k) for k in ('id','version')))


def reload_record(current, origin, previous_default, latest):
    """Refresh inherited values; retain explicit current-job edits and selections.

    origin is the last checked-file load's default, not a job snapshot. Missing
    origin means restored/copied history: preserve it until explicitly edited.
    Parameter preset, warehouse route and budget are independent edit groups.
    """
    if origin is None:
        return copy.deepcopy(current)
    bases = [origin] + ([previous_default] if previous_default else [])
    value = copy.deepcopy(latest)
    def parameter_key(record):
        p = record['preset']
        return p.get('id'), p.get('version'), p['config']
    if not any(parameter_key(current) == parameter_key(base) for base in bases):
        value['preset'] = copy.deepcopy(current['preset'])
    for keys in [('primary', 'secondary'), ('mode', 'amount')]:
        def group(record):return tuple(record['ui'].get(k) for k in keys)
        if not any(group(current) == group(base) for base in bases):
            for key in keys:value['ui'][key] = copy.deepcopy(current['ui'].get(key))
    value['ui']['selected'] = current['ui'].get('selected', True)
    return value
