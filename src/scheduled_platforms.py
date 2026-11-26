"""Existing application-owned browser leases, durable platform IDs, no UI controls."""
import time
import uuid
from pathlib import Path
from runtime_support import Cancelled, checkpoint
from aoxiang_jobs import Jobs
from aoxiang_acquisition import configuration_for_new, prepare, manifest, ACTIVE
from aoxiang_client import ComponentError
import catalog_sync_validation as validation
from source_archive import capture
from catalog_sync import stock_observation
from scheduled_replenishment import stamp


class Platforms:
    def __init__(self,folder,path,ax_manager,catalog_owner,cancel):
        self.folder,self.path=Path(folder),Path(path)
        self.ax_manager,self.catalog_owner=ax_manager,catalog_owner
        self.resources=getattr(catalog_owner,'resources',None)
        self.cancel=cancel;self.ax=None;self.jst=None;self.token=None;self.active_ax=None;self.active_jst=None

    def acquire(self):checkpoint(self.cancel)

    def stopped(self):return self.cancel.is_set() or bool(self.token and self.token.is_set())

    def ax_client(self):
        checkpoint(self.cancel)
        if self.ax is None:
            from unattended_resources import configured
            self.token=(self.ax_manager.gate.wait_background(self.cancel,lambda m:self.progress(m))
                        if configured(self.folder) else self.ax_manager.gate.claim_background())
            if self.token is None:raise ValueError('翱象正在人工操作或处理原任务，请稍后继续')
            self.ax=self.ax_manager.session.lease(cancelled=self.stopped);self.ax.start()
        return self.ax

    def jst_client(self,warehouses=None,progress=None):
        checkpoint(self.cancel)
        if self.jst is None:
            self.jst=(self.catalog_owner.acquire_wait(warehouses,self.cancel,progress,scheduled=True)
                      if self.resources and warehouses else self.catalog_owner.acquire())
            self.jst.start()
        return self.jst

    def release_ax(self,progress):
        if self.ax:
            while not self.ax.close(timeout=.5):
                progress('等待翱象当前动作安全结束，保留原任务编号');time.sleep(.5)
            self.ax=None
        if self.token is not None:self.ax_manager.gate.release_background(self.token);self.token=None

    def release(self,progress):
        self.release_ax(progress)
        self.release_jst(progress)

    def release_jst(self,progress):
        if self.jst:
            while not self.jst.close(timeout=.5):
                progress('等待商品资料当前动作安全结束，保留原任务编号');time.sleep(.5)
            self.jst=None

    def check_warehouse_bindings(self, warehouses):
        checkpoint(self.cancel)
        state = self.catalog_owner.read_status()
        accounts = {account['warehouse']: account for account in state.get('accounts', [])}
        for warehouse in warehouses:
            account = accounts.get(warehouse, {})
            has_identity = all(account.get(key) for key in ('expectedCompany', 'expectedCoid', 'expectedUid'))
            if state.get('rebind_required', {}).get(warehouse) or account.get('mappingVerified') is not True or not has_identity:
                raise ValueError(warehouse + '尚无有效的账号核验，请在「连接检查 / 修复」打开本仓配置')
            if not account.get('enabled'):
                raise ValueError(warehouse + '自动同步已停用，请在本仓配置中启用')

    def check_saved(self, basis, progress):
        """Use the shared components' saved bindings without borrowing either browser."""
        checkpoint(self.cancel)
        progress('读取已核验的翱象门店映射和聚水潭仓账号…')
        client = self.ax_manager.session.lease(cancelled=self.stopped, control_only=True)
        try:
            client.start()
            configuration = client.request('configuration')
        finally:
            client.close()
        if not configuration.get('configured') or not configuration.get('enabled'):
            raise ValueError('请在「连接检查 / 修复」核验并保存翱象连接与门店')
        verified = {target['buyerWarehouseCode'] for target in configuration['targets'] if target.get('mappingVerifiedAt')}
        missing = [f"{item['name']}（{item['code']}）" for item in basis['plan']['stores'] if item['code'] not in verified]
        if missing:
            raise ValueError('请在「翱象配置」核验这些门店：' + '、'.join(missing))
        self.check_warehouse_bindings(basis['warehouses'])

    def sales(self,runner,basis):
        self.progress=lambda message:runner.update('获取补货建议',message)
        repo=runner.repo;rid=runner.rid;run=repo.get(rid);external=dict(run['external'])
        jobs=Jobs(self.folder/'aoxiang'/'jobs.sqlite3')
        checkpoint(self.cancel)
        cid=external.get('ax');wanted=run.get('retry_codes');retry_id=run.get('retry_id')
        scope=[i['code'] for i in basis['plan']['stores']]
        if cid is None:
            if run['day']!=stamp().date().isoformat():
                raise ValueError('原任务尚未发起采集，已跨日，请按当天资料建立新任务')
            if wanted is not None and set(wanted)!=set(scope):
                raise ValueError('原任务尚未发起采集，请勾选全部未完成门店后继续')
            self.check_warehouse_bindings(basis['warehouses'])
        self.ax=self.ax_manager.session.lease(cancelled=self.stopped,control_only=True)
        self.ax.start();client=self.ax
        may_continue=True
        if cid is None:
            config=configuration_for_new(client,lambda message:runner.update('获取补货建议',message),self.cancel)
            job=prepare(jobs,config,scope,'refresh',day=run['day'])
            cid=job['id'];external['ax']=cid;repo.change(rid,epoch=runner.epoch,external=external)
            # Intent is linked before dispatch; same command has a durable idempotency contract.
            jobs.dispatch(client,cid);may_continue=False
        elif jobs.get(cid)['state']=='PREPARED':
            try:jobs.refresh(client,cid)
            except ComponentError as error:
                if error.code!='COMMAND_NOT_FOUND':raise
                if run['day']!=stamp().date().isoformat():
                    raise ValueError('原采集未受理且已跨日，请按当天资料建立新任务') from error
                if wanted is not None and set(wanted)!=set(scope):
                    raise ValueError('原采集未受理，请勾选全部未完成门店后继续') from error
                jobs.dispatch(client,cid);may_continue=False
        self.active_ax=cid;sent_stop=False;deadline=None
        while True:
            job=jobs.refresh(client,cid)
            if job['state'] not in ACTIVE:
                if not may_continue or self.stopped():break
                may_continue=False
                if self.continue_sales(client,jobs,job,wanted,retry_id,after_process_exit=run.get('recovering_process',False)):continue
                break
            waiting=job['state']=='QUEUED'
            if not retry_id or job['record'].get('selected_retry',{}).get('id')==retry_id:may_continue=False
            if not waiting and deadline is None:
                deadline=time.monotonic()+3600
            if (self.stopped() or deadline is not None and time.monotonic()>deadline) and not sent_stop:
                client.request('pause',command_id=cid);sent_stop=True
            items=job['record']['items'];done=sum(i['state'] in ('SUCCEEDED','FAILED','ENDED') for i in items)
            stage='等待浏览器可用' if waiting else '获取补货建议'
            detail=stage if waiting else job['record'].get('message') or '正在按本批范围取数'
            runner.update(stage,f'{detail} · 已处理 {done}/{len(items)} 家')
            time.sleep(.5)
        self.active_ax=None
        if sent_stop or self.stopped():raise Cancelled('取数已安全暂停；继续原任务')
        value=manifest(job,self.ax_manager.store.folder/'component-state',allow_empty=True)
        self.release_ax(runner.progress)
        return value

    @staticmethod
    def continue_sales(client,jobs,job,wanted,retry_id,*,after_process_exit=False):
        cid=job['id'];state=job['state'];items=job['record']['items']
        if wanted is not None and retry_id:
            indices=[i['index'] for i in items if i['warehouse_code'] in wanted]
            if len(indices)!=len(wanted):raise ValueError('原采集缺少所选门店，未派发重试')
            if all(items[index]['state']=='SUCCEEDED' for index in indices):return False
            receipt=client.request('collection.retry_selected',command_id=cid,indices=indices,
                                   sequence=job['sequence'],retry_id=retry_id)
        elif state in ('PAUSED','INTERRUPTED'):
            receipt=client.request('resume',command_id=cid)
        elif state=='NEEDS_REVIEW':
            try:receipt=client.request('collection.continue',command_id=cid)
            except ComponentError as error:
                # The per-store evidence is displayed below, even if no item has a usable receipt.
                if error.code!='COMMAND_TRANSITION':raise
                return False
        elif state=='COMPLETED_WITH_ERRORS':
            if after_process_exit:return False
            ids=[i['index'] for i in items if i['state']=='FAILED' and (wanted is None or i['warehouse_code'] in wanted)]
            if not ids:return False
            receipt=client.request('retry',command_id=cid,indices=ids,sequence=job['sequence'])
        else:return False
        jobs.observe(cid,receipt)
        return True

    def catalogs(self,runner,basis):
        repo=runner.repo;rid=runner.rid;run=repo.get(rid);external=dict(run['external'])
        recover_only=bool(run.get('day') and run['day']!=stamp().date().isoformat())
        if not external.get('catalog_operation'):
            if recover_only:
                raise ValueError('仓资料原采集尚未发起且已跨日，请建立当天任务')
            external['catalog_operation']=uuid.uuid4().hex
            external['catalog_items']=[{'warehouse':w,'expected_catalog_digest':basis['catalog_digests'][w]} for w in basis['warehouses']]
            repo.change(rid,epoch=runner.epoch,external=external)
        client=(self.jst_client([i['warehouse'] for i in external['catalog_items']],
                                lambda m:runner.update('更新仓商品资料',m)) if self.resources else self.jst_client())
        # A stable operation_id replays only the original durable intention, not a fresh export.
        response=client.request('sync.start',operation_id=external['catalog_operation'],purpose='scheduled_replenishment',items=external['catalog_items'],
                                independent_observation=True,recover_only=recover_only)
        bid=response['batch_id'];self.active_jst=bid;state=client.request('sync.status',batch_id=bid)
        if response.get('reused') and not state['running']:
            ids=[i['id'] for i in state['batches'][0]['items'] if not i.get('adoption') and not i.get('receipt')
                 and (not recover_only or (i.get('native') or {}).get('nativeTaskRef'))]
            if ids:client.request('sync.resume',batch_id=bid,item_ids=ids,**({'recover_only':True} if recover_only else {}))
        sent_stop=False;deadline=time.monotonic()+1800;observations=set()
        while True:
            state=client.request('sync.status',batch_id=bid);batch=state['batches'][0]
            if (self.cancel.is_set() or time.monotonic()>deadline) and not sent_stop:
                client.request('sync.stop',batch_id=bid);sent_stop=True
            waiting=False
            for item in batch['items']:
                if item['stage']=='WAIT_HOST' and not item.get('observation') and not sent_stop:
                    waiting=True;wh=item['account']['warehouse']
                    runner.update('更新仓商品资料','准备'+wh+'查询依据；尚未提交商品导出')
                    token=item['observationRequest']
                    if token not in observations:
                        checkpoint(self.cancel)
                        obs=stock_observation(self.path,wh)
                        checkpoint(self.cancel)
                        client.request('sync.provide_observation',batch_id=bid,item_id=item['id'],token=token,observation=obs)
                        observations.add(token)
            if state['active'] and not waiting:runner.update('更新仓商品资料',state['active']['phase'].get('message','等待平台导出'))
            if not state['running']:break
            time.sleep(.5)
        self.active_jst=None
        if sent_stop:raise Cancelled('仓资料已安全暂停，成功文件保留')
        catalogs={};states={}
        for item in batch['items']:
            checkpoint(self.cancel);wh=item['account']['warehouse'];name='catalog:'+wh
            if item.get('retirement'):raise ValueError('原仓采集已在纠正绑定时本地结束，请明确新建定时任务')
            prior=repo.value(rid,name)
            if prior is not None:
                from catalog_binding_guard import adoption_guard
                with adoption_guard(client.folder,batch,item,self.cancel,state_reader=lambda bid:client.request('sync.status',batch_id=bid)):
                    if (item.get('receipt') or {}).get('source_sha256')!=prior.get('source_sha256'):
                        raise ValueError(wh+'留存资料和原仓项回执不一致')
                    if not item.get('adoption'):
                        client.request('sync.acknowledge_adoption',batch_id=bid,item_id=item['id'],receipt={'warehouse':wh,'source_sha256':prior['source_sha256'],'purpose':'scheduled_replenishment','run_id':rid})
                catalogs[wh]=prior;states[wh]='本任务资料已留存';continue
            if not item.get('receipt'):states[wh]=str(item.get('error') or '采集未完成');continue
            receipt=item['receipt'];account=item['account'];native=item.get('native') or {}
            if (native.get('scope',{}).get('coid'),native.get('scope',{}).get('uid'))!=(account['expectedCoid'],account['expectedUid']):raise ValueError('原采集账号与仓绑定不一致')
            if receipt['warehouse']!=wh or native.get('sha256')!=receipt['source_sha256']:raise ValueError('仓与文件校验值不符')
            root=(client.folder/'downloads'/bid/item['id']).resolve()
            if any(not Path(receipt[k]).resolve().is_relative_to(root) for k in ('source','candidate')):raise ValueError('仓资料不在原任务目录')
            catalog=validation.candidate(receipt)
            from catalog_binding_guard import adoption_guard
            with adoption_guard(client.folder,batch,item,self.cancel,state_reader=lambda bid:client.request('sync.status',batch_id=bid)):
                capture(repo.path,catalog['source_path'],catalog['source_sha256'],runner.progress,self.cancel)
                from scheduled_replenishment import signature
                repo.save_value(rid,name,catalog,runner.epoch,adoption={
                    'source':'refresh','operation_id':external['catalog_operation'],'batch_id':bid,'item_id':item['id'],
                    'coid':account['expectedCoid'],'uid':account['expectedUid'],'binding_digest':signature(account.get('binding',{}))})
                ack={'warehouse':wh,'source_sha256':catalog['source_sha256'],'purpose':'scheduled_replenishment','run_id':rid}
                client.request('sync.acknowledge_adoption',batch_id=bid,item_id=item['id'],receipt=ack)
            catalogs[wh]=catalog;states[wh]='本任务资料已留存，工作台空闲时发布为当前仓资料'
        repo.change(rid,epoch=runner.epoch,warehouses=states)
        self.release_jst(lambda message:runner.update('更新仓商品资料',message))
        return catalogs
