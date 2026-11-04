"""Reusable ordered store lists and owned parameter values, independent of jobs."""
import copy,json,sqlite3,uuid
from contextlib import closing
import parameter_schema as schema
import system_standard,restock_policy
from work_repository import Conflict,now,dumps

def complete(config):
    # Existing schema already requires all common fields; only explicitly optional
    # policy keys are expanded here. Never convert numeric values implicitly.
    value=copy.deepcopy(schema.validate(config))
    for wh,cycle in system_standard.standard()['warehouses'].items():value['warehouses'].setdefault(wh,cycle)
    value[restock_policy.KEY]=restock_policy.values(value)
    return value

def validate(value):
    from purchase_io import code
    if not isinstance(value,dict) or not str(value.get('name','')).strip():raise ValueError('请填写计划名称')
    if not value.get('stores'):raise ValueError('请至少选择一家门店')
    seen=set()
    for item in value['stores']:
        code(item['code'])
        if item['code'] in seen:raise ValueError('计划门店重复：'+item['code'])
        if not isinstance(item.get('name'),str) or not item['name'].strip():raise ValueError('计划门店名称不能为空')
        seen.add(item['code'])
    schema.validate(value['config'])
    if 'parameter_fields' in value:
        import parameter_maintenance as pm
        allowed=set(pm.fields([value['config']])+pm.fields([value['config']],'common'))
        chosen=[tuple(f) for f in value['parameter_fields']]
        if len(chosen)!=len(set(chosen)) or set(chosen)-allowed:raise ValueError('预案包含不支持的参数范围')
    return value

class Repository:
    def __init__(self,path):self.path=path
    def list(self):
        with closing(sqlite3.connect(self.path)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='replenishment_plans'").fetchone():return []
            return [validate(json.loads(row[0])) for row in db.execute('SELECT payload FROM replenishment_plans ORDER BY created,id')]
    def save(self,value,expected=None):
        from storage_chunks import backup_before_write
        value=copy.deepcopy(value);value['config']=complete(value['config']);validate(value)
        value=json.loads(dumps(value))  # Same JSON containers as subsequent reloads.
        stamp=now();value.update(id=expected['id'] if expected else uuid.uuid4().hex,revision=expected['revision']+1 if expected else 1,
                                updated=stamp,created=expected['created'] if expected else stamp,standard_version=system_standard.VERSION)
        backup_before_write(self.path,lambda _:None)
        with closing(sqlite3.connect(self.path,timeout=10)) as db,db:
            db.execute('BEGIN IMMEDIATE');self.schema(db)
            if expected is not None:self.check(db,expected)
            db.execute('INSERT INTO replenishment_plans VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',(value['id'],value['created'],dumps(value)))
            self.event(db,'保存',expected,value)
        return copy.deepcopy(value)
    def delete(self,expected):
        from storage_chunks import backup_before_write
        backup_before_write(self.path,lambda _:None)
        with closing(sqlite3.connect(self.path,timeout=10)) as db,db:
            db.execute('BEGIN IMMEDIATE');self.schema(db);self.check(db,expected)
            db.execute('DELETE FROM replenishment_plans WHERE id=?',(expected['id'],));self.event(db,'删除',expected,None)
    @staticmethod
    def schema(db):
        db.execute('CREATE TABLE IF NOT EXISTS replenishment_plans(id TEXT PRIMARY KEY,created TEXT NOT NULL,payload TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS replenishment_plan_events(id INTEGER PRIMARY KEY,at TEXT,action TEXT,before_value TEXT,after_value TEXT)')
    @staticmethod
    def check(db,expected):
        r=db.execute('SELECT payload FROM replenishment_plans WHERE id=?',(expected['id'],)).fetchone()
        if not r or json.loads(r[0])!=expected:raise Conflict('计划已被其他窗口更新或删除，请刷新后重新核对')
    @staticmethod
    def event(db,action,before,after):db.execute('INSERT INTO replenishment_plan_events(at,action,before_value,after_value) VALUES(?,?,?,?)',(now(),action,dumps(before),dumps(after)))

def preset(plan):
    validate(plan)
    return {'id':'plan:'+plan['id'],'name':plan['name'],'version':plan['revision'],'builtin':False,
            'config':copy.deepcopy(plan['config']),'base_config':copy.deepcopy(plan['config']),
            'plan_source':{k:copy.deepcopy(plan[k]) for k in ('id','name','revision','updated','standard_version')}}

def ordered(stores,plan):
    return {s:stores[s] for s in dict.fromkeys([*(i['code'] for i in plan['stores']),*stores]) if s in stores}

def available(host):
    from aoxiang_directory import Directory
    data=Directory(host.work_repository.path.parent/'aoxiang').load()
    stores={i['code']:i['name'] for i in (data or {}).get('items',[])}
    stores.update({s:r.get('name') or s for s,r in host.work_repository.stores().items()})
    stores.update((host.loaded or {}).get('stores',{}));return stores

def apply(host,plan):
    """Explicit adoption only. Routes and budgets survive; no file fetching."""
    validate(plan)
    import parameter_maintenance as pm
    mapping=pm.mapping_snapshot(host.work_repository.path)
    if mapping['conflict']:raise ValueError('请先核对并保存全局分类映射，再采用预案')
    for s in host.store_widgets:
        if s in {i['code'] for i in plan['stores']}:
            c=pm.plan_config(plan,host.config_for(s));pm.bind({**preset(plan),'config':c},mapping)
    host.active_plan=copy.deepcopy(plan)
    if host.loaded:
        old={s:host.current_parameter_record(s) for s in host.store_widgets}
        host.loaded={**host.loaded,'stores':ordered(host.loaded['stores'],plan)}
        host._hydrating=True
        try:
            host.populate_stores();host.initializing=True
            for s,record in old.items():
                host.store_presets[s]=record['preset'];host.overrides[s]=copy.deepcopy(record['preset']['config']['warehouses']);host.set_store_ui(s,record['ui'])
            install(host,set(old),replace=True)
        finally:host.initializing=False;host._hydrating=False
        host.invalidate([i['code'] for i in plan['stores'] if i['code'] in old]);host.selection_changed()
    refresh(host);host.touch()

def install(host,previous,replace=False):
    plan=getattr(host,'active_plan',None)
    if not plan:return
    import parameter_maintenance as pm
    mapping=pm.mapping_snapshot(host.work_repository.path)
    if mapping['conflict']:host._mapping_error='请先在全局分类映射核对旧门店差异';return
    codes={i['code'] for i in plan['stores']}
    prepared={}
    try:
        for s in host.store_widgets:
            if s in codes and (replace or s not in previous):
                p=preset(plan);p['config']=pm.plan_config(plan,host.config_for(s));prepared[s]=pm.bind(p,mapping)
    except ValueError as exc:host._mapping_error='预案参数待核对：'+str(exc);return
    for s in host.store_widgets:
        if s in codes and (replace or s not in previous):
            p=prepared[s]
            host.store_presets[s]=p;host.overrides[s]=copy.deepcopy(p['config']['warehouses'])
            host._store_parameter_origins.pop(s,None)
        if replace or s not in previous:host.store_widgets[s]['check'].setChecked(s in codes and not host.loaded['blocked'].get(s))
        host.refresh_parameter_label(s)

def refresh(host):
    if not hasattr(host,'plan_note'):return
    plan=getattr(host,'active_plan',None)
    if not plan:host.plan_note.setText('未采用补货计划；可直接导入本次文件。');return
    missing=[i['name'] for i in plan['stores'] if i['code'] not in host.store_widgets]
    saved=host.work_repository.stores();unset=[i['name'] for i in plan['stores'] if not (host.store_ui(i['code']) if i['code'] in host.store_widgets else saved.get(i['code'],{}).get('ui',{})).get('primary')]
    cycle='；'.join(w+f' 备货{v[0]:g}天/到货{v[1]:g}天' for w,v in plan['config']['warehouses'].items())
    if 'parameter_fields' in plan:cycle=f"采用 {len(plan['parameter_fields'])} 项勾选参数；其他保留各店原值"
    names='、'.join(i['name'] for i in plan['stores'][:6])+(' 等'+str(len(plan['stores']))+'店（悬停看完整顺序）' if len(plan['stores'])>6 else '')
    host.plan_note.setToolTip('\n'.join(f"{n+1}. {i['name']} · {i['code']}" for n,i in enumerate(plan['stores'])))
    host.plan_note.setText(f"计划：{plan['name']} · {len(plan['stores'])} 店 · {cycle}\n"+names+'\n'+
        ('缺少补货文件：'+'、'.join(missing) if missing else '计划门店文件齐全')+('；供货关系待设：'+'、'.join(unset) if unset else ''))

def persist(path,value,expected=None,progress=lambda _:None,cancel=None):
    from runtime_support import checkpoint
    checkpoint(cancel);progress('备份并保存补货计划…');return Repository(path).save(value,expected)

def remove(path,value,progress=lambda _:None,cancel=None):
    from runtime_support import checkpoint
    checkpoint(cancel);progress('备份并删除所选计划，保留历史快照…');Repository(path).delete(value)
