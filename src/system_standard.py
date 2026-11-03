"""Versioned restoration baseline. No persistence API can write these values."""
import copy
import hashlib
import json
from app_defaults import load_default_config, DEFAULT_WAREHOUSES

VERSION = '2026-11-03.1'
# Canonical content, not platform-specific line endings. Set when baseline is sealed.
EXPECTED = '354caf60ce8dea2cc93301f035c5c63d422f83f75418df50739f36e1ea7a6607'

def standard():
    source = load_default_config()
    value = {k: source[k] for k in ('parameters', 'weight_profiles', 'category_profiles')}
    value.update(warehouses=copy.deepcopy(DEFAULT_WAREHOUSES), exception=True)
    actual = hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if actual != EXPECTED:
        raise ValueError('系统标准参数完整性检查失败，请使用完整交付包；未采用其他参数代替')
    return value

def restore(config, group, key=None):
    """Explicit value restoration, never persistence or implicit parent inheritance."""
    out = copy.deepcopy(config)
    fixed = standard()
    if group == 'profile_parameters':
        if key is None:
            from parameter_schema import PROFILE_KEYS
            out[group]={name:{k:fixed['parameters'][k] for k in PROFILE_KEYS} for name in out['weight_profiles']}
        else:
            name, field = key
            out.setdefault(group,{}).setdefault(name,{})[field]=fixed['parameters'][field]
    elif group == 'restock_switches':
        out[group] = {'extreme_restore':True,'new_stocking':True}
    elif group in fixed:
        if key is None: out[group] = copy.deepcopy(fixed[group])
        elif isinstance(key,tuple):
            name,field=key
            if name not in fixed[group]: raise ValueError('此分类没有系统标准，不能凭空恢复')
            out[group][name][field]=fixed[group][name][field]
        elif key in fixed[group]: out[group][key]=copy.deepcopy(fixed[group][key])
        elif group=='category_profiles':out[group].pop(key,None)
        else: raise ValueError('此项目没有系统标准')
    else: raise ValueError('此分组不属于参数恢复范围')
    return out
