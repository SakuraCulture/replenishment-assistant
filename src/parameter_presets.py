"""Named parameter presets, explicit references and frozen adoption snapshots.

Only future defaults/plans are updated here. Jobs and signed orders are never
read or written. Old detached store configurations remain detached.
"""
import copy
import hashlib
import uuid
from contextlib import closing
import parameter_schema as schema
from work_repository import Conflict, dumps, loads, now

MODE = 'per_store_presets'


def signature(config):
    return hashlib.sha256(dumps({k:v for k,v in config.items() if k!='category_profiles'}).encode()).hexdigest()


def bound(preset):
    p=copy.deepcopy(preset)
    p['default_binding']={'id':p['id'],'version':p['version'],'signature':signature(p['config'])}
    return p


def detach_if_changed(record):
    r=copy.deepcopy(record)
    if r is None:return None
    p=r['preset'];binding=p.get('default_binding')
    if binding and binding['signature']!=signature(p['config']):
        p.pop('default_binding',None)
    return r


def read(db):
    presets=[{'id':i,'version':v,'name':n,'config':loads(c),'builtin':False,'archived':bool(a)}
             for i,v,n,c,a in db.execute('SELECT id,version,name,config,archived FROM presets ORDER BY id,version DESC')]
    stores={s:loads(raw) for s,raw in db.execute('SELECT code,data FROM stores')}
    exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='replenishment_plans'").fetchone()
    plans=[loads(r[0]) for r in db.execute('SELECT payload FROM replenishment_plans ORDER BY created,id')] if exists else []
    data={'presets':presets,'stores':stores,'plans':plans}
    data['token']=hashlib.sha256(dumps(data).encode()).hexdigest()
    return data


def snapshot(repo):
    with closing(repo.connect()) as db:
        db.execute('BEGIN');return read(db)


def latest(data):
    values={'standard':{'id':'standard','version':1,'name':'标准补货','config':schema.standard(),'builtin':True}}
    for p in data['presets']:
        if not p['archived'] and p['id'] not in values:values[p['id']]={k:copy.deepcopy(v) for k,v in p.items() if k!='archived'}
    return values


def resolve(data,pid):
    if pid not in latest(data):raise ValueError('参数预案已删除或停用，请重新选择：'+str(pid))
    return copy.deepcopy(latest(data)[pid])


def references(data,pid):
    stores=[s for s,r in data['stores'].items() if r['preset'].get('default_binding',{}).get('id')==pid]
    plans=[(p,i) for p in data['plans'] if p.get('parameter_mode')==MODE for i in p['stores'] if i.get('preset_id')==pid]
    return stores,plans


def check(data,expected):
    if expected is not None and data['token']!=expected['token']:raise Conflict('参数预案、门店默认或补货预案已变化，请刷新后重新核对')


def write_stores(db,before,after,action):
    from settings_history import record
    for s,r in after.items():db.execute('INSERT INTO stores VALUES(?,?) ON CONFLICT(code) DO UPDATE SET data=excluded.data',(s,dumps(r)))
    record(db,before,after,action)


def save(repo,name,config,pid=None,expected=None):
    schema.validate(config);name=name.strip()
    if not name or len(name)>60:raise ValueError('方案名称须为1～60字')
    if pid=='standard':raise ValueError('系统默认参数不可修改，请复制另存')
    pid=pid or uuid.uuid4().hex
    with closing(repo.connect()) as db,db:
        db.execute('BEGIN IMMEDIATE');data=read(db);check(data,expected)
        version=db.execute('SELECT COALESCE(MAX(version),0)+1 FROM presets WHERE id=?',(pid,)).fetchone()[0]
        p={'id':pid,'version':version,'name':name,'config':copy.deepcopy(config),'builtin':False}
        db.execute('INSERT INTO presets(id,version,name,config) VALUES(?,?,?,?)',(pid,version,name,dumps(config)))
        codes,_=references(data,pid);before={s:data['stores'][s] for s in codes};after=copy.deepcopy(before)
        for r in after.values():
            old_mapping=r['preset']['config']['category_profiles']
            r['preset']=bound(p);r['preset']['config']['category_profiles']=old_mapping;r['saved']=now()
        write_stores(db,before,after,'更新参数预案：'+name)
    return p


def assign(repo,codes,pid,expected):
    from store_parameter_import import base
    with closing(repo.connect()) as db,db:
        db.execute('BEGIN IMMEDIATE');data=read(db);check(data,expected);p=resolve(data,pid)
        before={s:data['stores'].get(s) for s in codes};after={}
        for s in codes:
            r=copy.deepcopy(before[s] or base());mapping=r['preset']['config']['category_profiles']
            r['preset']=bound(p);r['preset']['config']['category_profiles']=mapping;r['saved']=now();after[s]=r
        write_stores(db,before,after,'选择门店默认参数预案：'+p['name'])
    return after


def delete(repo,pid,replacement,expected):
    from replenishment_plans import Repository
    if pid=='standard':raise ValueError('系统默认参数不可删除')
    if replacement==pid:raise ValueError('替换预案不能是待删除预案')
    from storage_chunks import backup_before_write
    backup_before_write(repo.path,lambda _:None)
    with closing(repo.connect()) as db,db:
        db.execute('BEGIN IMMEDIATE');data=read(db);check(data,expected);resolve(data,pid)
        codes,refs=references(data,pid)
        if (codes or refs) and not replacement:raise ValueError('预案仍被门店或补货预案使用，请选择替换预案')
        p=resolve(data,replacement) if replacement else None
        before={s:data['stores'][s] for s in codes};after=copy.deepcopy(before)
        for r in after.values():
            mapping=r['preset']['config']['category_profiles'];r['preset']=bound(p);r['preset']['config']['category_profiles']=mapping;r['saved']=now()
        write_stores(db,before,after,'删除参数预案并替换引用')
        for old in data['plans']:
            if not any(plan['id']==old['id'] for plan,_ in refs):continue
            new=copy.deepcopy(old)
            for item in new['stores']:
                if item.get('preset_id')==pid:item['preset_id']=replacement
            new['revision']+=1;new['updated']=now()
            db.execute('UPDATE replenishment_plans SET payload=? WHERE id=?',(dumps(new),new['id']))
            Repository.event(db,'替换已删除参数预案',old,new)
        # Versions remain available to old snapshots/history, absent from choices.
        db.execute('UPDATE presets SET archived=1 WHERE id=?',(pid,))


def freeze(plan,repo,*,data=None):
    if plan.get('parameter_mode')!=MODE:return copy.deepcopy(plan)
    if '_resolved_presets' in plan:return copy.deepcopy(plan)
    data=snapshot(repo) if data is None else data;p=copy.deepcopy(plan);resolved={}
    from store_parameter_import import base
    for item in p['stores']:
        s=item['code'];pid=item.get('preset_id')
        value=resolve(data,pid) if pid else copy.deepcopy((data['stores'].get(s) or base())['preset'])
        value['parameter_source']={'kind':'补货预案指定' if pid else '门店默认','plan_name':plan['name'],'store':s,
                                   'preset_id':value['id'],'preset_name':value['name'],'preset_version':value['version']}
        resolved[s]=value
    p['_resolved_presets']=resolved
    return p
