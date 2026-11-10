"""Bounded platform import pipeline over one serial, shared browser transport."""
import time
from aoxiang_client import ComponentError
from aoxiang_directory import Directory
from aoxiang_purchase_tasks import validate_for_upload
from aoxiang_order_guard import require_orders
from aoxiang_workflow_runner import SESSION_ERRORS
from runtime_support import checkpoint,Cancelled
from deletion_barrier import SourceDeleted
from aoxiang_purchase_messages import observation


def run_upload(runner,job):
    store,client=runner.store,runner.client
    limit=job.get('max_inflight',2)
    if type(limit) is not int or not 1<=limit<=4:raise ValueError('本批平台处理上限无效')
    scope=job.get('dispatch_keys');deadline=time.monotonic()+runner.verification_wait
    def current():return store.get(job['id'])
    def eligible(item):return scope is None or item['key'] in scope
    def update(item,**values):
        basis=item.get('head') or item.get('basis') or {}
        return store.update(job['id'],item_id=item['item_id'],oid=basis.get('oid'),execution_token=runner.execution_token,changes=values)
    def fail(item,error):
        code=getattr(error,'code','BUSINESS_REVIEW')
        update(item,state='PENDING' if code in SESSION_ERRORS else 'REVIEW',error={'code':code,'message':str(error),**({'details':error.details} if getattr(error,'details',None) else {})})
        if code in SESSION_ERRORS:raise error
    try:
        # Older serial batches may have been interrupted with an item RUNNING.
        # Resolve its durable component identity, rather than marking it done or
        # assuming that the old physical upload never happened.
        for item in current()['items']:
            if eligible(item) and item['state']=='RUNNING':update(item,state='PENDING')
        while True:
            checkpoint(runner.cancel)
            items=[i for i in current()['items'] if eligible(i)]
            waiting=[i for i in items if i['state']=='ACCEPTED']
            ready=[i for i in items if i['state']=='PENDING']
            # Fill the import window before spending any time waiting on the
            # first task's import completion or D16/D17 verification exports.
            for item in ready[:max(0,limit-len(waiting))]:
                checkpoint(runner.cancel)
                try:
                    require_orders(runner.business_path,[item]);p=item['package']
                    r=client.request('purchase.status',purchase_id=p['id'])
                    update(item,purchase_observation=observation(r))
                    if r['state']=='BOUND':update(item,state='BOUND',result={'binding':r['binding']},error=None);continue
                    if r['state']=='SUPERSEDED':raise ComponentError('PURCHASE_SUPERSEDED','原任务已重建，请打开后继任务')
                    if r.get('additionalTaskIds'):raise ComponentError('MULTIPLE_IMPORT_TASKS','原导入任务不唯一，请核对原采购记录')
                    if r['uploadAttempted'] and not r.get('taskId'):raise ComponentError('SUBMISSION_UNKNOWN','原上传结果未知；可核对原任务或明确结束并重建')
                    if not r['uploadAttempted']:
                        configuration=client.request('configuration');suppliers=Directory(store.folder).suppliers()
                        validate_for_upload(runner.business_path,p,configuration,suppliers,cancel=runner.cancel)
                    client.request('purchase.submit',purchase_id=p['id'])
                    r=runner.wait_status('purchase.status','purchase_id',p['id'],cancel_action='purchase.pause',
                        notify=lambda r,item=item:update(item,purchase_observation=observation(r)))
                    if r['state']=='BOUND':update(item,state='BOUND',result={'binding':r['binding']},error=None)
                    elif r.get('taskId') and not r.get('additionalTaskIds'):
                        update(item,state='ACCEPTED',purchase_observation=observation(r),error=None)
                    else:
                        e=r.get('error') or {};raise ComponentError(e.get('code','SUBMISSION_UNKNOWN'),e.get('message','上传未取得唯一回执；本单待核，其他订单继续'))
                except SourceDeleted:
                    basis=item.get('head') or item.get('basis') or {}
                    store.forget_missing(job['id'],item['item_id'],basis.get('oid'),runner.execution_token,runner.business_path)
                except Cancelled:raise
                except Exception as exc:fail(item,exc)
            waiting=[i for i in current()['items'] if eligible(i) and i['state']=='ACCEPTED']
            for item in waiting:
                checkpoint(runner.cancel)
                try:
                    require_orders(runner.business_path,[item]);pid=item['package']['id']
                    client.request('purchase.start',purchase_id=pid)
                    r=runner.wait_status('purchase.status','purchase_id',pid,cancel_action='purchase.pause',
                        notify=lambda r,item=item:update(item,purchase_observation=observation(r)))
                    if r['state']=='BOUND':update(item,state='BOUND',result={'binding':r['binding']},error=None)
                    else:
                        e=r.get('error') or {}
                        if e.get('code') in {'TASK_PENDING','TASK_NOT_FOUND','VERIFY_INTERRUPTED'} and r.get('taskId'):
                            runner.progress('平台正在处理采购；已受理任务保留，按本批上限继续调度')
                        else:raise ComponentError(e.get('code','PURCHASE_NEEDS_REVIEW'),e.get('message','原采购结果待核；其他订单继续'),e.get('details'))
                except SourceDeleted:
                    basis=item.get('head') or item.get('basis') or {}
                    store.forget_missing(job['id'],item['item_id'],basis.get('oid'),runner.execution_token,runner.business_path)
                except Cancelled:raise
                except Exception as exc:fail(item,exc)
                # A completed task frees a slot: submit another order before
                # checking a second potentially slow native proof.
                now=[i for i in current()['items'] if eligible(i)]
                if any(i['state']=='PENDING' for i in now) and sum(i['state']=='ACCEPTED' for i in now)<limit:break
            remaining=[i for i in current()['items'] if eligible(i) and i['state'] in ('PENDING','ACCEPTED')]
            if not remaining:break
            if any(i['state']=='PENDING' for i in remaining) and sum(i['state']=='ACCEPTED' for i in remaining)<limit:continue
            if time.monotonic()>=deadline:
                return store.update(job['id'],execution_token=runner.execution_token,state='QUEUED',next_poll_at=store.clock()+runner.verification_interval,
                                    message='平台处理中；稍后继续原任务，等待时释放共享会话供其他作业使用')
            if runner.cancel.wait(min(runner.verification_interval,max(0,deadline-time.monotonic()))):raise Cancelled('已安全暂停，保留全部受理任务；继续时不重传')
    except SourceDeleted:return None
    current_job=current()
    outside=any(i['state'] in ('PENDING','ACCEPTED') and not eligible(i) for i in current_job['items'])
    from aoxiang_workflow_state import summary
    state='PAUSED' if outside else 'COMPLETED_WITH_ERRORS' if any(i['state'] in ('FAILED','REVIEW') for i in current_job['items']) else 'COMPLETED'
    return store.update(job['id'],execution_token=runner.execution_token,state=state,next_poll_at=0,message=summary({**current_job,'state':state}))
