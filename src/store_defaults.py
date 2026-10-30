"""Durable store/warehouse parameters; daily budget persistence is explicit."""
import copy
from app_defaults import WAREHOUSES
import parameter_schema as schema

def record(preset,config,ui,old=None,*,include_budget=False,saved=''):
    schema.validate(config)
    primary,secondary=ui.get('primary'),ui.get('secondary')
    if primary not in (None,*WAREHOUSES) or secondary not in (None,*WAREHOUSES) or secondary and (not primary or primary==secondary):raise ValueError('主从仓设置无效，请先调整')
    durable={k:ui.get(k) for k in ('primary','secondary')}
    previous=(old or {}).get('ui',{})
    for key,default in [('mode',0),('amount','')]:durable[key]=ui.get(key,default) if include_budget else previous.get(key,default)
    # Inclusion in a batch is not a store default. Old records still load compatibly.
    durable['selected']=True
    return {'preset':{**copy.deepcopy(preset),'config':copy.deepcopy(config)},'ui':durable,'saved':saved}

def matches(saved,preset,config,ui):
    if not saved:return False
    return (saved['preset']['config']==config and
            all(saved['ui'].get(k)==ui.get(k) for k in ('primary','secondary')) and
            all(saved['preset'].get(k)==preset.get(k) for k in ('id','version')))
