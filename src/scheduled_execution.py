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
import priority_list
from runtime_support import Cancelled, checkpoint
import scheduled_snapshots as snapshots


def freeze(folder, schedule, day):
    """Read persisted business defaults, never the active window's temporary values."""
    import replenishment_plans as plans
    import warehouse_catalog, parameter_maintenance as pm
    import store_parameter_import as spi
    import admission, new_product_focus
    from workbench_actions import plan_record, record_settings
    work=wr.Repository(folder);path=work.path
    from parameter_presets import freeze as freeze_parameters,snapshot as parameter_snapshot
    parameter_data=parameter_snapshot(work)
    plan=next((p for p in parameter_data['plans'] if p['id']==schedule['plan_id']),None)
    if plan is None:raise ValueError('补货预案已删除，请重新选择')
    plan=freeze_parameters(plan,work,data=parameter_data)
    mapping=pm.mapping_snapshot(path)
    if mapping['conflict']:raise ValueError('全局分类映射待核对，请在参数设置保存后再试')
    priority=priority_list.Repository(folder);master=warehouse_catalog.Repository(folder)
    defaults=parameter_data['stores'];routes=master.routes();profile=priority_list.catalog_profile({});budgets=priority.budgets()
    monthly_focus=new_product_focus.Repository(folder).load()
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
            record=plan_record(plan,record,mapping,s);records[s]=record
            try:
                from new_products import calculation_settings
                settings[s]=calculation_settings(record_settings(record,profile,catalogs,admission.snapshot(path,s,db=db,day=day)),monthly_focus,day)
            except ValueError as exc:raise ValueError(item['name']+'（'+s+'）：'+str(exc)) from exc
            for w in (record['ui'].get('primary'),record['ui'].get('secondary')):
                if w and w not in warehouses:warehouses.append(w)
    if not warehouses:raise ValueError('预案门店尚未设置供货仓')
    from version_info import MODEL_VERSION, VERSION
    from deletion_barrier import read_epochs
    generations=read_epochs(path,day,records)
    from deletion_barrier import read_warehouse_epochs
    from app_defaults import WAREHOUSES
    warehouse_generations=read_warehouse_epochs(path,day,{s:WAREHOUSES for s in records})
    return {'deletion_generations':generations,'warehouse_deletion_generations':warehouse_generations,'plan':plan,'records':records,'jobs':settings,'profile':profile,'new_focus':monthly_focus,'mapping':mapping,
            'warehouses':warehouses,'catalog_digests':{w:master.current_digest(w) for w in warehouses},
            'day':day,'model_version':MODEL_VERSION,'app_version':VERSION,'workers':schedule['workers']}


def check_signature(frozen):
    # Legacy seal for arrangements that did not retain their store scope.
    value=copy.deepcopy(frozen)
    for k in ('day','catalog_digests','app_version'):value.pop(k,None)
    for job in value['jobs'].values():
        job['admission'].pop('day',None)
        job.pop('warehouse_new_day',None)
        if job.get('monthly_first_fill'):job['monthly_first_fill'].pop('day',None)
    return sr.signature(value)


def matches_check_signature(frozen, expected, folder):
    if check_signature(frozen)==expected:
        return True
    # Older schedules included the global list in the setup seal. Verify
    # that original seal, then calculate new runs from their own warehouse facts.
    try:
        previous=priority_list.Repository(folder).profile(create_default=False)
    except (OSError, ValueError):
        return False
    return check_signature({**frozen,'profile':previous})==expected


def require_arranged_stores(frozen, schedule):
    current = {item['code'] for item in frozen['plan']['stores']}
    approved = {item['code'] for item in schedule.get('stores', [])}
    if current != approved:
        raise ValueError('补货预案的门店范围已变化，请编辑安排，确认门店后保存')


def adopted_catalogs(repo,rid,warehouses):
    from warehouse_catalog import validate
    previous=repo.value(rid,'catalogs',{})
    receipts=repo.get(rid).get('catalog_adoptions',{})
    catalogs={}
    for wh in warehouses:
        catalog=repo.value(rid,'catalog:'+wh)
        legacy=previous.get(wh)
        if catalog and legacy and catalog!=legacy:raise wr.Conflict(wh+'原任务仓资料留存互相冲突，请核对原件')
        catalog=catalog or legacy
        if not catalog:
            if wh in receipts:raise wr.Conflict(wh+'缺少原采用快照，请核对原任务')
            continue
        validate(catalog)
        if catalog['warehouse']!=wh or wh in receipts and any(
                catalog[key]!=receipts[wh].get(key) for key in ('revision','source_sha256')):
            raise wr.Conflict(wh+'仓资料与原采用回执不符，请核对原任务')
        catalogs[wh]=catalog
    return catalogs


class PendingInputs:
    def __init__(self, runner, manifest, catalogs, allowed):
        from warehouse_catalog import product_table
        self.runner, self.catalogs = runner, catalogs
        self.files, self.scopes, self.hashes = {}, {}, {}
        for item in manifest['files']:
            code, path, digest = item['warehouse_code'], item['file'], item['sha256']
            if code not in allowed or self.hashes.setdefault(path, digest)!=digest:
                raise ValueError('已验收原文件的门店范围或校验值冲突')
            self.files.setdefault(code, {})[path]=digest
            self.scopes.setdefault(path, set()).add(code)
        self.tables = {wh: product_table(wh, catalog) for wh, catalog in catalogs.items()}
        self.archived = set()
        self.digests = runner.repo.digests(runner.rid)

    def __call__(self, code):
        from file_intake import check_file
        from source_archive import capture
        from performance_log import begin
        r=self.runner
        name='input:'+code
        if name in self.digests:
            loaded=r.repo.value(r.rid,name,expected=self.digests[name])
        else:
            paths=self.files[code]
            checked=[]
            for index,(path,digest) in enumerate(paths.items()):
                checkpoint(r.cancel)
                value=check_file(index,path,store=code,allowed=self.scopes[path],expected=digest,cancel=r.cancel)
                if value['report']['error']:
                    raise ValueError(f'{code} · {Path(path).name}：{value["report"]["error"]}')
                checked.append(value)
                if (path,digest) not in self.archived:
                    capture(r.repo.path,path,digest,r.progress,r.cancel)
                    self.archived.add((path,digest))
            intake=batch_service.intake(paths,self.catalogs,checked=checked)
            # Source identity has been checked around the read and archived by hash.
            intake['frozen']=True
            if not intake['selected'] and intake['conflicts']:
                from warehouse_catalog import metadata
                loaded={'intake':intake,'stores':intake['stores'],'normalized':[],
                        'blocked':{code:len(intake['conflicts'])},'warehouses':list(self.catalogs),
                        'config':batch_service.engine.config(),'hashes':intake['hashes'],
                        'catalogs':{w:metadata(c) for w,c in self.catalogs.items()}}
            else:
                loaded=batch_service.prepare(intake,cancel=r.cancel,catalog_tables=self.tables)
            head=snapshots.input_head(loaded)
            loaded['intake']={k:v for k,v in loaded['intake'].items() if k!='catalogs'}
            with begin('calculation.persist_store_input'):
                r.repo.save_value(r.rid,name,loaded,r.epoch,input_head=head)
        common,stores=batch_service.calculation_inputs(loaded,{code:None},True)
        return {**common,**stores[code]}


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

    def defaults(self,warehouses,day):
        from warehouse_catalog import Repository
        from source_archive import capture
        if self.repo.clock().astimezone(sr.TZ).date().isoformat()!=day:
            raise ValueError('原任务未采用'+ '、'.join(warehouses)+'资料且已跨日，请建立当天任务')
        catalogs,digests=Repository(self.folder).snapshots(warehouses)
        for wh,catalog in catalogs.items():
            checkpoint(self.cancel)
            if Path(catalog['source_path']).is_file():
                capture(self.repo.path,catalog['source_path'],catalog['source_sha256'],self.progress,self.cancel)
            self.repo.save_value(self.rid,'catalog:'+wh,catalog,self.epoch,
                                 adoption={'source':'default','default_digest':digests[wh]})
        return catalogs

    def execute(self,rid):
        run=self.repo.claim(rid);self.rid=rid;self.epoch=run['epoch']
        resources=getattr(self.backend,'resources',None)
        try:
            frozen=self.repo.value(rid,'basis')
            if frozen is None:
                schedule = run['schedule']
                if not sr.has_verified_bindings(schedule):
                    raise ValueError('该安排尚未核验门店和仓账号，请从执行安排保存并启用')
                frozen=freeze(self.folder,schedule,run['day'])
                if schedule.get('stores'):
                    require_arranged_stores(frozen, schedule)
                elif not matches_check_signature(frozen, schedule.get('check_signature'), self.folder):
                    raise ValueError('旧安排的门店范围无法核对，请编辑安排，确认门店后保存')
                self.repo.change(rid,epoch=self.epoch,stores=[{**i,'state':'PENDING','message':'等待处理'} for i in frozen['plan']['stores']])
                self.repo.save_value(rid,'basis',frozen,self.epoch)
            wanted=run.get('retry_codes')
            stores=self.repo.get(rid)['stores']
            if run.get('recovering_process') and stores and all(s['state']=='READY' for s in stores):
                self.update('等待人工核对',f'{len(stores)}/{len(stores)} 家已计算并保存，等待核对',
                            state='COMPLETE',retry_codes=None,retry_id=None,recovering_process=False)
                return
            selected=[s['code'] for s in stores if s['state']!='READY' and (wanted is None or s['code'] in wanted)]
            with self.repo.db() as db:
                sr.check_unsigned(db,run['day'],selected)
            from version_info import MODEL_VERSION
            if frozen['model_version']!=MODEL_VERSION:raise ValueError('模型版本变化，原任务仅供查看，请建立新任务')
            checkpoint(self.cancel)
            self.backend.acquire()
            self.update('获取补货建议','按预案门店顺序获取；成功文件留存，失败项单独处理')
            manifest=self.repo.value(rid,'sales')
            if not manifest or not set(selected)<={f['warehouse_code'] for f in manifest['files']}:
                manifest=self.backend.sales(self, frozen)
                self.repo.save_value(rid,'sales',manifest,self.epoch)
            checkpoint(self.cancel)
            available={f['warehouse_code'] for f in manifest['files']}
            omitted={s['warehouse_code']:s for s in manifest.get('omitted',[])}
            for s in selected:
                if s in available:
                    continue
                failure=omitted.get(s,{})
                error=failure.get('error') or {}
                reason=error.get('message') or '补货建议未取得，请核对原采集任务'
                if failure.get('task_id'):
                    reason+=f"（原任务 {failure['task_id']}）"
                self.repo.store_state(rid,s,'FAILED',reason,self.epoch)
            selected=[s for s in selected if s in available]
            catalogs=adopted_catalogs(self.repo,rid,frozen['warehouses'])
            missing=[wh for wh in frozen['warehouses'] if wh not in catalogs]
            if selected and missing:
                self.update('更新仓商品资料','正在核对本次任务的仓资料')
                if run['schedule'].get('catalog_source')=='default':
                    catalogs.update(self.defaults(missing,run['day']))
                else:
                    acquired=self.backend.catalogs(self,frozen)
                    if any(wh in catalogs and catalogs[wh]!=data for wh,data in acquired.items()):
                        raise wr.Conflict('新返回仓资料与本次已采用版本不同，请核对原仓项')
                    catalogs.update(acquired)
                checkpoint(self.cancel)
            eligible=[]
            for s in selected:
                missing=[w for w in frozen['jobs'][s]['supply_route'].values() if w and w not in catalogs]
                if missing:self.repo.store_state(rid,s,'FAILED','仓资料未完成：'+'、'.join(missing),self.epoch)
                else:eligible.append(s)
            if eligible:
                workers=frozen['workers']
                if resources:
                    from unattended_resources import configured
                    if configured(self.folder):
                        workers=min(workers,2)
                        while not resources.try_heavy(self,self.repo.path.parent):
                            self.update('等待本地作业','计算等待本地名额、内存和资料卷余量')
                            self.cancel.wait(.5);checkpoint(self.cancel)
                self.update('检查文件','逐店准备待算门店，保留已验收原件及原行号')
                inputs=PendingInputs(self,manifest,catalogs,frozen['records'])
                if 'catalogs' not in inputs.digests or missing:
                    self.repo.save_value(rid,'catalogs',catalogs,self.epoch)
                jobs={s:frozen['jobs'][s] for s in eligible}
                self.update('计算门店',f'同时计算最多{workers}家；运行中的门店安全完成后暂停')
                persistence=[]
                def notify(s,state,result,error):
                    if result is not None:
                        try:self.repo.save_value(rid,'result:'+s,result,self.epoch,store=s,
                                                 review=snapshots.projection(result,frozen.get('new_focus')))
                        except Exception as exc:persistence.append(exc);self.cancel.set();raise
                    else:
                        self.repo.store_state(rid,s,{'计算中':'RUNNING','等待内存':'WAITING_MEMORY',
                                                  '已停止':'PENDING'}.get(state,'FAILED'),error or state,self.epoch)
                    self.progress(s+' · '+state)
                profile=frozen['profile']
                if priority_list.is_catalog_profile(profile):
                    profile=priority_list.catalog_profile(catalogs)
                batch_service.run(None,jobs,profile,workers,notify,self.cancel,input_for=inputs,retain_results=False,
                                  system=getattr(self.backend,'commit_sample',None))
                if resources:resources.release_heavy(self)
                if persistence:raise persistence[0]
            checkpoint(self.cancel)
            run=self.repo.get(rid);all_ready=all(s['state']=='READY' for s in run['stores'])
            done=sum(s['state']=='READY' for s in run['stores'])
            message=f'{done}/{len(run["stores"])} 家已计算并保存，等待核对'
            if not all_ready:message+='。请查看待处理门店的原因'
            fields={'publication':{'state':'USING_DEFAULT'}} if run['schedule'].get('catalog_source')=='default' else {}
            self.update('等待人工核对',message,state='COMPLETE' if all_ready else 'REVIEW',retry_codes=None,retry_id=None,recovering_process=False,**fields)
        except Cancelled:
            run=self.repo.get(rid)
            self.update('已安全停止','成功结果已保存；继续时沿用原任务和冻结依据',state='ENDED' if run.get('control')=='end' else 'PAUSED')
        except Exception as exc:
            import logging
            logging.exception('定时补货运行未完成 %s',rid)
            self.update('需要处理',str(exc),state='REVIEW')
        finally:
            if resources:resources.release_heavy(self)
            self.backend.release(self.progress)


def workbench_record(repo,rid):
    """Use the existing restore/save/sign path. Opening is explicit and never saves a draft."""
    from version_info import VERSION
    run=repo.get(rid);basis=repo.value(rid,'basis')
    if not basis:raise ValueError('尚无通过检查的数据，请先处理失败原因')
    catalogs=adopted_catalogs(repo,rid,basis['warehouses'])
    digests=repo.digests(rid)
    loaded=snapshots.input_view(repo,run,catalogs,digests)
    if 'loaded' in digests:
        # Older partial runs retain their original input for already ready stores.
        legacy=repo.value(rid,'loaded')
        loaded=snapshots.merge_legacy_inputs(legacy,loaded)
    if not basis or not loaded:raise ValueError('尚无通过检查的数据，请先处理失败原因')
    entries={s['code']:s for s in run['stores'] if s['state']=='READY'}
    results=snapshots.Results({s:snapshots.Reference(repo.path,rid,'result:'+s,digests['result:'+s]) for s in entries},
                              {s:item['review'] for s,item in entries.items() if 'review' in item})
    if not results:raise ValueError('尚无计算完成的门店')
    records=basis['records'];codes=[s['code'] for s in run['stores'] if s['code'] in loaded['stores']]
    loaded['stores']={s:loaded['stores'][s] for s in codes}
    profile=basis['profile']
    if priority_list.is_catalog_profile(profile):
        profile=priority_list.catalog_profile(catalogs)
    payload={'deletion_generations':basis.get('deletion_generations',{}),'warehouse_deletion_generations':basis.get('warehouse_deletion_generations',{}),'schema':1,'day':run['day'],'app_version':VERSION,'model_version':basis['model_version'],
        'active_plan':basis['plan'],'admission_required':True,'loaded':loaded,'catalogs':catalogs,
        'profile':profile,'new_focus':basis.get('new_focus'),'results':results,
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
    from warehouse_catalog import Repository,WAREHOUSES,stock_publication_warning
    from catalog_write_lock import acquire
    from contextlib import ExitStack
    run=repo.get(rid)
    if run['state'] not in ('COMPLETE','REVIEW','PAUSED'):raise ValueError('请等待本次计算安全结束后采用资料')
    if run['schedule'].get('catalog_source')=='default':
        return '本任务已固定当时的默认仓资料，供本次计算使用',{}
    if run['day']!=sr.stamp().date().isoformat():
        message='原业务日的已采用仓资料继续留存，当前仓资料等待当日采集'
        repo.change(rid,publication={'state':'HISTORICAL_ONLY'},message=message)
        return message,{}
    basis=repo.value(rid,'basis');catalogs=repo.value(rid,'catalogs',{})
    if not basis or not catalogs:raise ValueError('没有可采用的资料')
    master=Repository(folder);publication=run.get('publication',{})
    done=dict(publication.get('digests',{}));intents=dict(publication.get('intents',{}))
    desired={wh:master.publication_digest(data) for wh,data in catalogs.items()}
    issues={wh:warning for wh,data in catalogs.items() if wh not in done
            and (warning:=stock_publication_warning(data))}
    with ExitStack() as locks:
        for wh in WAREHOUSES:
            if wh in catalogs:locks.enter_context(acquire(folder,wh,None))
        for wh in catalogs:
            current=master.current_digest(wh)
            if wh not in done and intents.get(wh)==desired[wh] and current==desired[wh]:
                done[wh]=current
            if current!=done.get(wh,basis['catalog_digests'][wh]):
                error=wh+'当前仓资料已变化，请从资料维护核对，本次原资料已保留'
                repo.change(rid,publication={'state':'NEEDS_REVIEW','digests':done,'intents':intents,'errors':{wh:error}},message=error)
                raise wr.Conflict(error)
        for wh,data in catalogs.items():
            if wh not in done and wh not in issues:
                intents[wh]=desired[wh]
                repo.change(rid,publication={'state':'PUBLISHING','digests':done,'intents':intents})
                if master.current_digest(wh)!=desired[wh]:
                    master.save_locked(data,expected_digest=basis['catalog_digests'][wh])
                done[wh]=desired[wh]
    if issues:
        message=('、'.join(done)+'已更新。' if done else '')+'仓资料发布待核：'+ '；'.join(wh+' '+error for wh,error in issues.items())
        repo.change(rid,publication={'state':'NEEDS_REVIEW','digests':done,'errors':issues},message=message)
        return message,master.snapshots(done)[0]
    repo.change(rid,publication={'state':'PUBLISHED','digests':done},message='当前仓资料已更新，计算结果等待人工核对')
    return '、'.join(catalogs)+'已更新当前资料',master.snapshots(catalogs)[0]
