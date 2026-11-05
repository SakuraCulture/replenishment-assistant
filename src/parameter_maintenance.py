"""Scoped parameter patches and one durable global classification mapping.

Model configs remain complete immutable snapshots. Shared defaults are resolved
only when starting a new job or explicitly adopting settings, never on calculation.
"""
import copy
import hashlib
import json
import sqlite3
from contextlib import closing
import parameter_schema as schema
import store_parameter_import as spi
import store_defaults
from work_repository import Conflict, dumps, now


def fields(configs, section='store'):
    configs = list(configs) or [schema.standard()]
    if section == 'common':
        return [('parameters', k) for k in schema.HELP if k not in spi.FIXED]
    result = []
    for c in configs:
        for wh in c['warehouses']:
            for i in (0, 1):
                f = ('warehouses', wh, i)
                if f not in result: result.append(f)
        for group in c['weight_profiles']:
            for kind, keys in [('weight_profiles', spi.WEIGHTS), ('profile_parameters', schema.PROFILE_KEYS)]:
                for key in keys:
                    f = (kind, group, key)
                    if f not in result: result.append(f)
    return result


def title(f):
    if f[0] == 'warehouses': return f[1] + ' / ' + ('备货天数', '到货天数')[f[2]]
    if f[0] == 'parameters': return schema.HELP[f[1]][0]
    return f[1] + ' / ' + (spi.WEIGHTS[f[2]] if f[0] == 'weight_profiles' else schema.HELP[f[2]][0])


def value(config, f):
    if f[0] == 'profile_parameters': return config.get(f[0], {}).get(f[1], {}).get(f[2], '沿用门店')
    node = config
    for k in f: node = node[k]
    return node


def default_value(f):
    return '沿用门店' if f[0] == 'profile_parameters' else value(schema.standard(), f)


def patch(config, changes):
    allowed = set(fields([config]) + fields([config], 'common'))
    if set(changes) - allowed: raise ValueError('参数设置范围无效；映射、预算、供货关系和策略请分别维护')
    return spi.patch({'values': changes}, spi.base({'id':'scoped','name':'分组设置','version':1,'config':config}))['preset']['config']


def common_is_default(config):
    return all(value(config, f) == default_value(f) for f in fields([config], 'common'))


def selection_data(codes, changes, names=None):
    if not changes: raise ValueError('请勾选至少一个需要修改的参数')
    return {'rows':[{'code':c, 'name':(names or {}).get(c,''), 'values':copy.deepcopy(changes),
                     'source':'界面勾选设置', 'duplicates':[]} for c in codes], 'sources':{},'scoped':True,
            'formats':['界面设置：仅修改勾选字段，未勾选项保持原值']}


def mapping_snapshot(path):
    """Read-only migration preview; never select an arbitrary store's mapping."""
    with closing(sqlite3.connect(path)) as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='global_category_mapping'").fetchone()
        row = db.execute('SELECT payload FROM global_category_mapping WHERE id=1').fetchone() if exists else None
        stores = {c:store_defaults.decode(json.loads(raw)) for c,raw in db.execute('SELECT code,data FROM stores')}
    variants = {}
    for code, store in stores.items():
        if not store_defaults.has_parameters(store):continue
        mapping = store['preset']['config']['category_profiles']
        key = dumps(mapping)
        variants.setdefault(key, {'mapping':mapping, 'stores':[]})['stores'].append(code)
    variants = list(variants.values())
    current = json.loads(row[0]) if row else None
    mapping = current['mapping'] if current else variants[0]['mapping'] if len(variants)==1 else schema.standard()['category_profiles'] if not variants else None
    # Bind preview to settings and the stored global revision, not unrelated jobs.
    token = hashlib.sha256(dumps({'current':current, 'stores':stores}).encode()).hexdigest()
    return {'current':current, 'mapping':copy.deepcopy(mapping), 'variants':variants,
            'token':token, 'stores':stores, 'conflict':mapping is None}


def validate_mapping(mapping, configs=()):
    if not isinstance(mapping, dict) or any(not isinstance(k,str) or not k.strip() or k!=k.strip() or not isinstance(v,str) or not v.strip() for k,v in mapping.items()):
        raise ValueError('商品分类和目标分类组不能为空，分类名称不能重复')
    for c in list(configs) or [schema.standard()]:
        missing = set(mapping.values()) - set(c['weight_profiles'])
        if missing: raise ValueError('部分门店缺少分类组：'+'、'.join(sorted(missing))+'；请先维护这些分类组的参数')
    return copy.deepcopy(mapping)


def save_mapping(path, mapping, expected, progress=lambda _:None, cancel=None):
    from runtime_support import checkpoint
    from storage_chunks import backup_before_write
    checkpoint(cancel)
    validate_mapping(mapping, [r['preset']['config'] for r in expected['stores'].values()])
    backup_before_write(path, progress)
    checkpoint(cancel)
    with closing(sqlite3.connect(path,timeout=10)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        # Second read uses the locked connection to avoid preview/commit races.
        present=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='global_category_mapping'").fetchone()
        raw=db.execute('SELECT payload FROM global_category_mapping WHERE id=1').fetchone() if present else None
        stores={c:store_defaults.decode(json.loads(s)) for c,s in db.execute('SELECT code,data FROM stores')}
        current=json.loads(raw[0]) if raw else None
        token=hashlib.sha256(dumps({'current':current,'stores':stores}).encode()).hexdigest()
        if token!=expected['token']: raise Conflict('全局映射或门店默认已变化，请重新核对')
        checkpoint(cancel)
        data={'revision':(current or {}).get('revision',0)+1,'saved':now(),'mapping':copy.deepcopy(mapping)}
        db.execute('CREATE TABLE IF NOT EXISTS global_category_mapping(id INTEGER PRIMARY KEY,payload TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS global_category_mapping_events(id INTEGER PRIMARY KEY,at TEXT,before_value TEXT,after_value TEXT)')
        db.execute('INSERT OR REPLACE INTO global_category_mapping VALUES(1,?)',(dumps(data),))
        db.execute('INSERT INTO global_category_mapping_events(at,before_value,after_value) VALUES(?,?,?)',(now(),dumps(current),dumps(data)))
    return data


def bind(preset, snapshot):
    if snapshot['conflict']: raise ValueError('各店旧分类映射不一致，请在“全局分类映射”核对并保存统一映射')
    p=copy.deepcopy(preset)
    p['config']['category_profiles']=validate_mapping(snapshot['mapping'],[p['config']])
    p['mapping_source']={'scope':'全局分类映射','revision':(snapshot['current'] or {}).get('revision',0),
                         'digest':hashlib.sha256(dumps(snapshot['mapping']).encode()).hexdigest()}
    return p


def plan_config(plan, original):
    """Old plans retain their explicit complete parameter selection; mappings don't."""
    chosen=plan.get('parameter_fields')
    if chosen is None:
        out=copy.deepcopy(plan['config'])
        out['category_profiles']=copy.deepcopy(original['category_profiles'])
        return schema.validate(out)
    changes={tuple(f):value(plan['config'],tuple(f)) for f in chosen}
    return patch(original,changes)
