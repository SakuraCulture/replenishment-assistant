"""Analysis-owned intentions over shared native command journals. Never publish business masters."""
from pathlib import Path
import time
import uuid
from stockout_common import now, today, file_hash, resource_check
from stockout_facts import load_catalog, load_store
from runtime_support import Cancelled, checkpoint


class Client:
    def __init__(self, rpc, service):
        self.rpc,self.service=rpc,service
    def request(self, action, **values):
        return self.rpc(self.service, action=action, values=values)


def refresh_job(jobs,client,cid):
    current=jobs.get(cid)
    record=client.request('status',command_id=cid)
    if current.get('record')!=record:
        jobs.observe(cid,record)
    return jobs.get(cid)


def collect_catalogs(repo,rid,rpc,cancel,progress):
    run=repo.run(rid);basis=repo.settings(run['basis'])
    warehouses=sorted({w for route in basis['routes'].values() for w in (route['primary'],route.get('secondary')) if w})
    if not warehouses:return {}
    external=dict(run['external']);client=Client(rpc,'jst')
    if not external.get('catalog_operation'):
        external.update(catalog_operation=uuid.uuid4().hex,catalog_owner=rid,
                        catalog_items=[{'warehouse':w,'expected_catalog_digest':None} for w in warehouses])
        repo.change(rid,external=external)
    checkpoint(cancel)
    result=client.request('sync.start',operation_id=external['catalog_operation'],purpose='stockout_analysis',items=external['catalog_items'])
    bid=result['batch_id'];state=client.request('sync.status',batch_id=bid)
    if result.get('reused') and not state['running']:
        missing=[i['id'] for i in state['batches'][0]['items'] if not i.get('receipt') and not i.get('adoption')]
        if missing:client.request('sync.resume',batch_id=bid,item_ids=missing)
    stop=False;deadline=time.monotonic()+1800
    while True:
        state=client.request('sync.status',batch_id=bid);batch=state['batches'][0]
        if (cancel.is_set() or time.monotonic()>deadline) and not stop:
            client.request('sync.stop',batch_id=bid);stop=True
        for item in batch['items']:
            if item['stage']=='WAIT_HOST' and not item.get('observation') and not stop:
                progress('准备'+item['account']['warehouse']+'分析资料查询依据')
                observation=rpc('observation',warehouse=item['account']['warehouse'])
                client.request('sync.provide_observation',batch_id=bid,item_id=item['id'],token=item['observationRequest'],observation=observation)
        if not state['running']:break
        time.sleep(.4)
    if stop or cancel.is_set():raise Cancelled('仓资料采集已安全暂停，继续原任务即可')
    catalogs=dict(run['catalogs'])
    root=Path(rpc('jst_root')).resolve()
    for item in batch['items']:
        checkpoint(cancel);wh=item['account']['warehouse'];receipt=item.get('receipt')
        if not receipt:
            progress(wh+'资料未取得；该仓相关分层将标记待核')
            continue
        account=item['account'];native=item.get('native') or {}
        if (native.get('scope',{}).get('coid'),native.get('scope',{}).get('uid'))!=(account['expectedCoid'],account['expectedUid']):
            raise ValueError('仓原件账号与仓绑定不一致，已停止；请在连接配置核对账号')
        if receipt['warehouse']!=wh or native.get('sha256')!=receipt['source_sha256']:
            raise ValueError('仓原件与原采集回执不一致，未采用')
        expected_root=root/'downloads'/bid/item['id']
        if any(not Path(receipt[key]).resolve().is_relative_to(expected_root) for key in ('source','candidate')):
            raise ValueError('仓资料不在原任务目录，未采用')
        # Reuse the complete native validator and receipt integrity contract.
        from catalog_sync_validation import candidate
        validated=candidate(receipt)
        del validated
        sha=load_catalog(repo,receipt['source'],wh,expected=receipt['source_sha256'],expected_rows=receipt['rows'],cancel=cancel,progress=progress)
        catalogs[wh]=sha
        repo.change(rid,catalogs=catalogs)
        ack={'warehouse':wh,'source_sha256':sha,'purpose':'stockout_analysis','run_id':external['catalog_owner']}
        if not item.get('adoption'):client.request('sync.acknowledge_adoption',batch_id=bid,item_id=item['id'],receipt=ack)
        elif item['adoption']!=ack:raise ValueError('分析原件曾被其他用途采用，请核对原任务')
    external=repo.run(rid)['external'];external['catalogs_pending']=any(w not in catalogs for w in warehouses)
    repo.change(rid,external=external)
    rpc('release_jst')
    return catalogs


def execute(repo,rid,rpc,cancel,progress=lambda _:None):
    from aoxiang_jobs import Jobs
    from aoxiang_acquisition import prepare,manifest,ACTIVE
    run=repo.run(rid)
    if run['state'] in {'COMPLETE','PARTIAL','ENDED'}:
        raise ValueError('已发布任务请创建新修订，不可覆盖')
    repo.change(rid,state='RUNNING',message='核对分析已采用设置及平台身份')
    try:
        basis=repo.settings(run['basis']);checkpoint(cancel);resource_check(repo.folder)
        client=Client(rpc,'ax');configuration=client.request('configuration')
        if not configuration.get('configured') or not configuration.get('enabled'):
            raise ValueError('翱象连接尚未启用，请在原连接配置完成核验')
        available={s['buyerWarehouseCode'] for s in configuration['targets']}
        if any(s['code'] not in available for s in basis['stores']):
            raise ValueError('分析所选门店已不在核验绑定中，请核对门店身份后重新采用设置')
        external=dict(run['external']);identity={'account':configuration['account_binding'],'mapping':configuration['store_mapping_version']}
        if external.get('identity') and external['identity']!=identity:
            raise ValueError('连接身份或门店绑定已变更，不能把当前账号数据接入原任务；请核对原任务')
        external['identity']=identity;repo.change(rid,external=external)
        rpc('release_ax')
        # A resumed run keeps successful catalogs. Never silently switch its basis.
        if not external.get('catalogs_frozen'):
            collect_catalogs(repo,rid,rpc,cancel,progress)
            external=repo.run(rid)['external'];external['catalogs_frozen']=not external.get('catalogs_pending',False);repo.change(rid,external=external)
        jobs=Jobs(repo.root/'aoxiang'/'jobs.sqlite3')
        root=Path(rpc('ax_root'))
        for store in basis['stores']:
            checkpoint(cancel);resource_check(repo.folder);code=store['code'];current=repo.run(rid)
            row=next(s for s in current['stores'] if s['store']==code)
            if row['state']=='READY':continue
            external=current['external'];commands=dict(external.get('ax_commands',{}));cid=commands.get(code)
            progress('采集 '+store['name']+'；只生成分析快照')
            try:
                if cid is None:
                    job=prepare(jobs,configuration,[code],'refresh',day=today(),purpose='stockout_analysis',owner=rid)
                    cid=job['id'];commands[code]=cid;external['ax_commands']=commands;repo.change(rid,external=external)
                    jobs.dispatch(client,cid)
                else:
                    job=jobs.get(cid)
                    if job['context'].get('purpose')!='stockout_analysis' or job['command']['scope']['warehouse_codes']!=[code]:
                        raise ValueError('原命令用途或门店不符，未重试')
                    if job['state']=='PREPARED':jobs.dispatch(client,cid)
                    else:
                        job=refresh_job(jobs,client,cid)
                        if job['state'] in ('PAUSED','INTERRUPTED'):
                            jobs.observe(cid,client.request('resume',command_id=cid))
                        elif job['state']=='COMPLETED_WITH_ERRORS':
                            indices=[i['index'] for i in job['record']['items'] if i['state']=='FAILED']
                            if indices:jobs.observe(cid,client.request('retry',command_id=cid,indices=indices,sequence=job['sequence']))
                stop=False;deadline=time.monotonic()+3600
                while True:
                    job=refresh_job(jobs,client,cid)
                    if job['state'] not in ACTIVE:break
                    if (cancel.is_set() or time.monotonic()>deadline) and not stop:
                        client.request('pause',command_id=cid);stop=True
                    time.sleep(.4)
                if stop or cancel.is_set():raise Cancelled('已安全暂停；已受理导出保留原任务，继续时先查询原结果')
                if not any(i['state']=='SUCCEEDED' for i in (job.get('record') or {}).get('items',[])):
                    reasons=[(i.get('error') or {}).get('message','') for i in (job.get('record') or {}).get('items',[])]
                    raise ValueError('原导出尚未取得可用原件：'+('；'.join(x for x in reasons if x) or job['state'])+'。保留原任务编号，继续时先核对原结果。')
                value=manifest(job,root)
                file=value['files'][0]
                load_store(repo,rid,code,file['file'],observed=file['collected_at'],expected=file['sha256'],expected_rows=file['rows'],cancel=cancel,progress=progress)
            except Cancelled:
                raise
            except Exception as exc:
                repo.store_result(rid,code,error=str(exc))
                progress(store['name']+'未完成：'+str(exc))
        checkpoint(cancel)
        return repo.publish(rid)
    except Cancelled as exc:
        repo.change(rid,state='PAUSED',message=str(exc));raise
    except Exception as exc:
        repo.change(rid,state='REVIEW',message=str(exc));raise


def finish_local(repo,rid,rpc,cancel):
    """End local analysis without losing outstanding native commands or enabling duplicates."""
    run=repo.run(rid)
    if run['state'] in {'COMPLETE','PARTIAL','ENDED'}:return run
    # No forced detach: shared outstanding commands must be reconciled first.
    from aoxiang_jobs import Jobs
    if run['external'].get('catalog_operation'):
        state=Client(rpc,'jst').request('sync.status')
        batch=next((b for b in state.get('batches',[]) if b.get('operation_id')==run['external']['catalog_operation']),None)
        if batch and any(not item.get('adoption') for item in batch['items']):
            raise ValueError('原仓导出尚未完成核对和留存，请继续原分析任务；不会解除原任务关联后重新导出')
    if run['external'].get('ax_commands'):
        jobs=Jobs(repo.root/'aoxiang'/'jobs.sqlite3');client=Client(rpc,'ax')
        for cid in run['external']['ax_commands'].values():
            checkpoint(cancel);job=refresh_job(jobs,client,cid)
            if job['state'] not in {'SUCCEEDED','COMPLETED_WITH_ERRORS','ENDED','CANCELLED'}:
                raise ValueError('原平台任务尚未结束，请先继续核对原任务；暂不解除防重保护')
    repo.change(rid,state='ENDED',message='已结束本地分析；成功原件及平台证据保留')
    return repo.run(rid)
