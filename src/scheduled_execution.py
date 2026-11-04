"""A bounded, resumable orchestration layer over the existing business services."""
import copy
import sqlite3
import threading
import time
from pathlib import Path
from contextlib import closing
import batch_service
import work_repository as wr
import scheduled_replenishment as sr
from runtime_support import Cancelled, checkpoint


def freeze(folder, schedule, day):
    """Read persisted business defaults, never the active window's temporary values."""
    import replenishment_plans as plans
    import priority_list, warehouse_catalog, parameter_maintenance as pm
    import store_parameter_import as spi
    import admission, new_product_focus
    from workbench_actions import plan_record, record_settings
    work=wr.Repository(folder);path=work.path
    plan=next((p for p in plans.Repository(path).list() if p['id']==schedule['plan_id']),None)
    if plan is None:raise ValueError('补货预案已删除，请重新选择')
    mapping=pm.mapping_snapshot(path)
    if mapping['conflict']:raise ValueError('全局分类映射待核对，请在参数设置保存后再试')
    priority=priority_list.Repository(folder);master=warehouse_catalog.Repository(folder)
    defaults=work.stores();routes=master.routes();profile=priority.profile();budgets=priority.budgets()
    # Only route names are needed before fresh catalogs arrive. Do not parse/copy
    # both full previous masters just to validate membership in the warehouse list.
    from app_defaults import WAREHOUSES
    catalogs=dict.fromkeys(WAREHOUSES);records={};settings={};warehouses=[]
    with closing(sqlite3.connect(path)) as db:
        db.execute('BEGIN')
        for item in plan['stores']:
            s=item['code'];record=copy.deepcopy(defaults.get(s))
            if record is None:
                record=spi.base();record['ui'].update(routes.get(s,{}))
                if s in budgets:record['ui'].update(mode=2 if budgets[s]['enabled'] else 1,amount=str(budgets[s]['amount']))
            record=plan_record(plan,record,mapping);records[s]=record
            try:settings[s]=record_settings(record,profile,catalogs,admission.snapshot(path,s,db=db,day=day))
            except ValueError as exc:raise ValueError(item['name']+'（'+s+'）：'+str(exc)) from exc
            for w in (record['ui'].get('primary'),record['ui'].get('secondary')):
                if w and w not in warehouses:warehouses.append(w)
    if not warehouses:raise ValueError('预案门店尚未设置供货仓')
    from version_info import MODEL_VERSION, VERSION
    return {'plan':plan,'records':records,'jobs':settings,'profile':profile,'new_focus':new_product_focus.Repository(folder).load(),'mapping':mapping,
            'warehouses':warehouses,'catalog_digests':{w:master.current_digest(w) for w in warehouses},
            'day':day,'model_version':MODEL_VERSION,'app_version':VERSION,'workers':schedule['workers']}


def check_signature(frozen):
    # Dates are occurrence-specific. Catalog files are deliberately refreshed on each run.
    value=copy.deepcopy(frozen)
    for k in ('day','catalog_digests','app_version'):value.pop(k,None)
    for job in value['jobs'].values():job['admission'].pop('day',None)
    return sr.signature(value)


def overlap(repo,run,codes):
    with repo.db() as db:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'normal_orders' in tables:
            signed={r[0] for r in db.execute('SELECT store FROM normal_orders WHERE day=?',(run['day'],))}
            if signed.intersection(codes):raise ValueError('本日已有签发：'+ '、'.join(s for s in codes if s in signed)+'；请人工核对，不自动覆盖')
        for raw, in db.execute("SELECT payload FROM scheduled_runs WHERE day=? AND rowid<(SELECT rowid FROM scheduled_runs WHERE id=?) AND state NOT IN ('ENDED','MISSED')",(run['day'],run['id'])):
            other=wr.loads(raw)
            # Later queued occurrences never block the earlier one.
            selected={i['code'] for i in other['schedule'].get('stores',other['stores'])}
            if selected.intersection(codes):raise ValueError('同日门店已有自动任务，请先核对或结束原任务：'+other['schedule']['name'])


class Runner:
    def __init__(self,repo,backend,folder,cancel=None,progress=lambda _:None):
        self.repo,self.backend,self.folder=repo,backend,Path(folder)
        self.cancel=cancel or threading.Event();self.progress=progress
        self.rid=None;self.epoch=None;self.last=''

    def update(self,stage,message,**fields):
        # Store only actual changes, not polling heartbeats.
        key=(stage,str(message))
        if key!=self.last or fields:
            self.repo.change(self.rid,epoch=self.epoch,stage=stage,message=str(message),**fields)
            self.last=key;self.progress(str(message))

    def execute(self,rid):
        run=self.repo.claim(rid);self.rid=rid;self.epoch=run['epoch']
        try:
            frozen=self.repo.value(rid,'basis')
            if frozen is None:
                frozen=freeze(self.folder,run['schedule'],run['day'])
                if check_signature(frozen)!=run['schedule'].get('check_signature'):
                    raise ValueError('预案、默认参数或准入依据已变化，请重新检查运行条件后新建任务')
                codes=[i['code'] for i in frozen['plan']['stores']];overlap(self.repo,run,codes)
                self.repo.change(rid,epoch=self.epoch,stores=[{**i,'state':'PENDING','message':'等待处理'} for i in frozen['plan']['stores']])
                self.repo.save_value(rid,'basis',frozen,self.epoch)
            from version_info import MODEL_VERSION
            if frozen['model_version']!=MODEL_VERSION:raise ValueError('模型版本变化，原任务仅供查看，请建立新任务')
            checkpoint(self.cancel)
            self.backend.acquire()
            self.update('获取补货建议','按预案门店顺序获取；成功文件留存，失败项单独处理')
            manifest=self.backend.sales(self, frozen)
            self.repo.save_value(rid,'sales',manifest,self.epoch)
            checkpoint(self.cancel)
            self.update('更新仓商品资料','本任务每仓获取一次，不修改聚水潭分配库存')
            catalogs=self.backend.catalogs(self,frozen)
            checkpoint(self.cancel)
            run=self.repo.get(rid);wanted=run.get('retry_codes')
            eligible=[]
            for item in run['stores']:
                s=item['code']
                if item['state']=='READY' or wanted is not None and s not in wanted:continue
                missing=[w for w in frozen['jobs'][s]['supply_route'].values() if w and w not in catalogs]
                if missing:self.repo.store_state(rid,s,'FAILED','仓资料未完成：'+'、'.join(missing),self.epoch)
                elif not any(f['warehouse_code']==s for f in manifest['files']):self.repo.store_state(rid,s,'FAILED','补货建议未取得，请查看原采集任务',self.epoch)
                else:eligible.append(s)
            if eligible:
                self.update('检查文件','按原规则检查已验收文件，保留冲突及来源顺序')
                paths=[f['file'] for f in manifest['files']]
                loaded=batch_service.load(paths,catalogs,lambda m:self.update('检查文件',m),self.cancel)
                from aoxiang_acquisition import verify_loaded
                verify_loaded(manifest,loaded)
                if set(loaded['stores'])-set(frozen['records']):raise ValueError('下载文件包含预案外门店，未自动采用')
                from source_archive import retained_import
                retained_import(batch_service.load,(),loaded,self.repo.path,lambda m:self.update('留存原件',m),self.cancel)
                self.repo.save_value(rid,'loaded',loaded,self.epoch)
                self.repo.save_value(rid,'catalogs',catalogs,self.epoch)
                jobs={s:frozen['jobs'][s] for s in eligible if s in loaded['stores']}
                for s in eligible:
                    if s not in jobs:self.repo.store_state(rid,s,'FAILED','文件没有该门店记录',self.epoch)
                self.update('计算门店',f'同时计算最多{frozen["workers"]}家；运行中的门店安全完成后暂停')
                persistence=[]
                def notify(s,state,result,error):
                    if result is not None:
                        try:self.repo.save_value(rid,'result:'+s,result,self.epoch,store=s)
                        except Exception as exc:persistence.append(exc);self.cancel.set();raise
                    else:self.repo.store_state(rid,s,'RUNNING' if state=='计算中' else 'FAILED',error or state,self.epoch)
                    self.progress(s+' · '+state)
                batch_service.run(loaded,jobs,frozen['profile'],frozen['workers'],notify,self.cancel)
                if persistence:raise persistence[0]
            checkpoint(self.cancel)
            run=self.repo.get(rid);all_ready=all(s['state']=='READY' for s in run['stores'])
            self.update('等待人工核对','已完成门店的结果已保存，可进入核对；尚未签发',state='COMPLETE' if all_ready else 'REVIEW')
        except Cancelled:
            run=self.repo.get(rid)
            self.update('已安全停止','成功结果已保存；继续时沿用原任务和冻结依据',state='ENDED' if run.get('control')=='end' else 'PAUSED')
        except Exception as exc:
            import logging
            logging.exception('定时补货运行未完成 %s',rid)
            self.update('需要处理',str(exc),state='REVIEW')
        finally:self.backend.release(self.progress)


def workbench_record(repo,rid):
    """Use the existing restore/save/sign path. Opening is explicit and never saves a draft."""
    from version_info import VERSION
    run=repo.get(rid);basis=repo.value(rid,'basis');loaded=repo.value(rid,'loaded')
    if not basis or not loaded:raise ValueError('尚无通过检查的数据，请先处理失败原因')
    results={s['code']:repo.value(rid,'result:'+s['code']) for s in run['stores'] if s['state']=='READY'}
    if not results:raise ValueError('尚无计算完成的门店')
    records=basis['records'];codes=[s['code'] for s in run['stores'] if s['code'] in loaded['stores']]
    loaded['stores']={s:loaded['stores'][s] for s in codes}
    payload={'schema':1,'day':run['day'],'app_version':VERSION,'model_version':basis['model_version'],
        'active_plan':basis['plan'],'admission_required':True,'loaded':loaded,'catalogs':repo.value(rid,'catalogs'),
        'profile':basis['profile'],'new_focus':basis.get('new_focus'),'results':results,
        'states':{s:{'name':loaded['stores'][s],'state':'已完成' if s in results else '计算失败','error':'' if s in results else '定时任务尚未完成；请在运行记录重试'} for s in codes},
        'input_dirty':False,'store_presets':{s:records[s]['preset'] for s in codes},'store_ui':{s:records[s]['ui'] for s in codes},
        'overrides':{},'default_by_store':{},'sales':[x['path'] for x in loaded['intake']['files']],
        'products':'','legacy':False,'concurrency':basis['workers'],'exception':True,'export_log':[],
        'scheduled_run':rid}
    return {'day':run['day'],'saved':run['updated'],'payload':payload},basis


def publish_catalogs(repo,rid,folder):
    """Publish only verified task masters, with all warehouse version checks first.

    Called by the GUI coordinator only while the workbench is idle. The task snapshot
    remains authoritative for its calculation even if a default publication is refused.
    Per-warehouse receipts permit safe local retry after a partial filesystem failure.
    """
    from warehouse_catalog import Repository
    from catalog_write_lock import acquire
    from contextlib import ExitStack
    run=repo.get(rid)
    if run['state'] not in ('COMPLETE','REVIEW','PAUSED'):raise ValueError('请等待本次计算安全结束后采用资料')
    basis=repo.value(rid,'basis');catalogs=repo.value(rid,'catalogs',{})
    if not basis or not catalogs:raise ValueError('没有可采用的资料')
    master=Repository(folder);done=dict(run.get('publication',{}).get('digests',{}))
    with ExitStack() as locks:
        for wh in sorted(catalogs):locks.enter_context(acquire(folder,wh,None))
        for wh in catalogs:
            current=master.current_digest(wh)
            if current!=done.get(wh,basis['catalog_digests'][wh]):
                raise ValueError(wh+'默认资料已变化，请从资料维护核对，本次未覆盖')
        for wh,data in catalogs.items():
            if wh not in done:
                master.save_locked(data,expected_digest=basis['catalog_digests'][wh]);done[wh]=master.current_digest(wh)
                repo.change(rid,publication={'state':'PUBLISHING','digests':done})
    repo.change(rid,publication={'state':'PUBLISHED','digests':done},message='本次仓资料已更新为默认；计算结果等待人工核对')
    return '、'.join(catalogs)+'已更新默认',master.catalogs()
