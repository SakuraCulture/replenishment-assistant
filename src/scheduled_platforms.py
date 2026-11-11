"""Existing application-owned browser leases, durable platform IDs, no UI controls."""
import time
import uuid
from pathlib import Path
from runtime_support import Cancelled, checkpoint
from aoxiang_jobs import Jobs
from aoxiang_acquisition import prepare, manifest, ACTIVE
import catalog_sync_validation as validation
from source_archive import capture
from catalog_sync import stock_observation


class Platforms:
    def __init__(self,folder,path,ax_manager,catalog_owner,cancel):
        self.folder,self.path=Path(folder),Path(path)
        self.ax_manager,self.catalog_owner=ax_manager,catalog_owner
        self.cancel=cancel;self.ax=None;self.jst=None;self.token=None;self.active_ax=None;self.active_jst=None

    def acquire(self):checkpoint(self.cancel)

    def stopped(self):return self.cancel.is_set() or bool(self.token and self.token.is_set())

    def ax_client(self):
        checkpoint(self.cancel)
        if self.ax is None:
            self.token=self.ax_manager.gate.claim_background()
            if self.token is None:raise ValueError('翱象正在人工操作或处理原任务，请稍后继续')
            self.ax=self.ax_manager.session.lease(cancelled=self.stopped);self.ax.start()
        return self.ax

    def jst_client(self):
        checkpoint(self.cancel)
        if self.jst is None:self.jst=self.catalog_owner.acquire();self.jst.start()
        return self.jst

    def release_ax(self,progress):
        if self.ax:
            while not self.ax.close(timeout=.5):
                progress('等待翱象当前动作安全结束，保留原任务编号');time.sleep(.5)
            self.ax=None
        if self.token is not None:self.ax_manager.gate.release_background(self.token);self.token=None

    def release(self,progress):
        self.release_ax(progress)
        if self.jst:
            while not self.jst.close(timeout=.5):
                progress('等待商品资料当前动作安全结束，保留原任务编号');time.sleep(.5)
            self.jst=None

    def preflight(self,basis,progress):
        """Read-only platform checks; refresh observed login, never export or change store mappings."""
        try:
            ax=self.ax_client();progress('正在核验翱象登录与预案门店；如需授权，请在浏览器允许')
            from aoxiang_current_login import current_configuration
            current_configuration(ax,progress,self.cancel)
            job=ax.request('setup.verify_saved',codes=[i['code'] for i in basis['plan']['stores']])
            ident=job['id'];deadline=time.monotonic()+600
            try:
                while job['state']=='RUNNING' or job.get('cleanup_pending'):
                    if self.stopped() or time.monotonic()>deadline:raise Cancelled('连接核验已停止或超时；未创建平台导出')
                    progress(job['stage']);time.sleep(.5);job=ax.request('setup.status',setup_id=ident)
                if job['state']!='VERIFIED' or any(i['state']!='VERIFIED' for i in job['targets']):raise ValueError(str(job.get('error') or '翱象门店未全部通过核验'))
            finally:ax.request('setup.cancel',setup_id=ident)
            self.release_ax(progress)
            client=self.jst_client()
            for wh in basis['warehouses']:
                checkpoint(self.cancel);progress('核验'+wh+'专用浏览器登录与账号')
                op=client.request('accounts.verify',warehouse=wh)['operation_id'];deadline=time.monotonic()+240
                while True:
                    if self.cancel.is_set() or time.monotonic()>deadline:
                        client.request('sync.stop');raise Cancelled('仓账号核验已停止或超时')
                    state=client.request('sync.status');item=state['operations'][op]
                    if item['state']!='RUNNING' and not state['running']:break
                    time.sleep(.5)
                if item['state']!='SUCCEEDED':raise ValueError(wh+'：'+str(item.get('error') or item['state']))
            return {'checked':True,'message':'翱象门店、两仓所需账号已通过只读核验；不会保证未来登录不失效'}
        finally:self.release(progress)

    def sales(self,runner,basis):
        repo=runner.repo;rid=runner.rid;run=repo.get(rid);external=dict(run['external'])
        jobs=Jobs(self.folder/'aoxiang'/'jobs.sqlite3');client=self.ax_client()
        cid=external.get('ax')
        if cid is None:
            from aoxiang_current_login import current_configuration
            config=current_configuration(client,lambda message:runner.update('获取补货建议',message),self.cancel)
            job=prepare(jobs,config,[i['code'] for i in basis['plan']['stores']],'refresh',day=run['day'])
            cid=job['id'];external['ax']=cid;repo.change(rid,epoch=runner.epoch,external=external)
            # Intent is linked before dispatch; same command has a durable idempotency contract.
            jobs.dispatch(client,cid)
        else:
            job=jobs.get(cid)
            if job['state']=='PREPARED':jobs.dispatch(client,cid)
            else:
                job=jobs.refresh(client,cid)
                if job['state'] in ('PAUSED','INTERRUPTED'):
                    jobs.observe(cid,client.request('resume',command_id=cid))
                elif job['state']=='COMPLETED_WITH_ERRORS':
                    wanted=run.get('retry_codes')
                    ids=[i['index'] for i in job['record']['items'] if i['state']=='FAILED' and (wanted is None or i['warehouse_code'] in wanted)]
                    if ids:jobs.observe(cid,client.request('retry',command_id=cid,indices=ids,sequence=job['sequence']))
        self.active_ax=cid;sent_stop=False;deadline=time.monotonic()+3600
        while True:
            job=jobs.refresh(client,cid)
            if job['state'] not in ACTIVE:break
            if (self.stopped() or time.monotonic()>deadline) and not sent_stop:
                client.request('pause',command_id=cid);sent_stop=True
            items=job['record']['items'];done=sum(i['state'] in ('SUCCEEDED','FAILED','ENDED') for i in items)
            runner.update('获取补货建议',f'已处理 {done}/{len(items)} 家；'+str(job['record'].get('message') or job['state']))
            time.sleep(.5)
        self.active_ax=None
        if sent_stop or self.stopped():raise Cancelled('取数已安全暂停；继续原任务')
        value=manifest(job,self.ax_manager.store.folder/'component-state')
        self.release_ax(runner.progress)
        return value

    def catalogs(self,runner,basis):
        repo=runner.repo;rid=runner.rid;run=repo.get(rid);external=dict(run['external']);client=self.jst_client()
        if not external.get('catalog_operation'):
            external['catalog_operation']=uuid.uuid4().hex
            external['catalog_items']=[{'warehouse':w,'expected_catalog_digest':basis['catalog_digests'][w]} for w in basis['warehouses']]
            repo.change(rid,epoch=runner.epoch,external=external)
        # A stable operation_id replays only the original durable intention, not a fresh export.
        response=client.request('sync.start',operation_id=external['catalog_operation'],purpose='scheduled_replenishment',items=external['catalog_items'])
        bid=response['batch_id'];self.active_jst=bid;state=client.request('sync.status',batch_id=bid)
        if response.get('reused') and not state['running']:
            ids=[i['id'] for i in state['batches'][0]['items'] if not i.get('adoption') and not i.get('receipt')]
            if ids:client.request('sync.resume',batch_id=bid,item_ids=ids)
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
                repo.save_value(rid,name,catalog,runner.epoch)
                ack={'warehouse':wh,'source_sha256':catalog['source_sha256'],'purpose':'scheduled_replenishment','run_id':rid}
                client.request('sync.acknowledge_adoption',batch_id=bid,item_id=item['id'],receipt=ack)
            catalogs[wh]=catalog;states[wh]='本任务资料已留存；默认资料待工作台空闲时更新'
        repo.change(rid,epoch=runner.epoch,warehouses=states)
        return catalogs
