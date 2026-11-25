"""Uncapped accepted imports over one serial, shared browser transport."""
import time
from aoxiang_client import ComponentError
from aoxiang_directory import Directory
from aoxiang_purchase_tasks import validate_for_upload
from aoxiang_order_guard import require_orders
from aoxiang_workflow_runner import SESSION_ERRORS
from runtime_support import checkpoint,Cancelled
from deletion_barrier import SourceDeleted
from aoxiang_purchase_messages import observation, import_failure, import_failure_message


def original_observation(item, record):
    previous = item.get('purchase_observation') or {}
    if record.get('id') != item['package']['id'] or (
            previous.get('taskId') and previous['taskId'] != record.get('taskId')) or (
            previous.get('uploadAttempted') and not record.get('uploadAttempted')):
        raise ComponentError('PURCHASE_SOURCE_CONFLICT', '组件回执与原采购任务不一致，原记录保留')
    return observation(record)


def original_units(path, item, record):
    """Units from the exact signed source, using the original stock-unit scope."""
    from lifecycle_facts import full_head

    head = item['head']
    basis = record.get('basis') or {}
    for field, frozen in [('oid', 'oid'), ('number', 'number'), ('warehouse', 'warehouse'),
                          ('store', 'store'), ('revision', 'revision'), ('digest', 'signedDigest')]:
        if head[field] != basis.get(frozen):
            raise ComponentError('PURCHASE_SOURCE_CONFLICT', '原仓单与采购任务的签发身份不一致：'+field)
    import hashlib
    import json
    from pathlib import Path

    package = item['package']
    payload = Path(package['package_file']).read_bytes()
    if hashlib.sha256(payload).hexdigest() != package['package_sha256']:
        raise ComponentError('PURCHASE_SOURCE_CONFLICT', '原上传任务文件摘要不符')
    intent = json.loads(payload)
    if intent['id'] != package['id'] or intent['basis'] != basis:
        raise ComponentError('PURCHASE_SOURCE_CONFLICT', '原上传任务文件与组件记录身份不一致')
    products = full_head(path, head)['products']
    skus = {row['sku'] for row in intent['rows']}
    return {sku: unit for sku, row in products.items()
            if sku in skus and isinstance(unit := row.get('unit'), str) and unit.strip()}


def run_verification(runner, job):
    """Only look up the listed original purchases. No upload path in this pass."""
    store, client, id = runner.store, runner.client, job['id']
    while True:
        checkpoint(runner.cancel)
        current = store.get(id)
        queue = current.get('verification_queue', [])
        if not queue:
            return store.finish_upload(id, runner.execution_token, 'COMPLETED', verification=True)
        key = queue[0]
        item = next((i for i in current['items'] if i['item_id'] == key), None)
        changes = {}
        if item:
            try:
                require_orders(runner.business_path, [item])
                pid = item['package']['id']
                record = client.request('purchase.status', purchase_id=pid)
                changes['purchase_observation'] = original_observation(item,record)
                if not record.get('taskId') and record['state'] != 'BOUND':
                    raise ComponentError('SUBMISSION_UNKNOWN', '原上传缺少可核实的唯一任务号；保留待核')
                units = original_units(runner.business_path, item, record)
                runner.progress(item['head']['number']+' · 核对原采购结果')
                client.request('purchase.verify', purchase_id=pid, original_units=units)
                record = runner.wait_status('purchase.status', 'purchase_id', pid,
                                            cancel_action='purchase.pause')
                changes['purchase_observation'] = original_observation({**item,**changes},record)
                error = record.get('verificationError') or record.get('error')
                if record['state'] != 'BOUND' or error:
                    error = error or {}
                    raise ComponentError(error.get('code', 'PURCHASE_NEEDS_REVIEW'),
                                         error.get('message', '原采购结果尚未核实'), error.get('details'))
                require_orders(runner.business_path, [item])
                changes.update(state='BOUND', result={'binding': record['binding']}, error=None)
            except Cancelled:
                raise
            except Exception as exc:
                # Preserve an earlier verified association and expose the new
                # query failure independently. A failed query proves no void.
                from aoxiang_workflow_runner import item_error
                changes.update(state='BOUND' if item['state']=='BOUND' else 'REVIEW', error=item_error(exc))
        store.settle_verification(id, key, runner.execution_token, changes)


def run_upload(runner,job):
    store,client=runner.store,runner.client
    # Persisted max_inflight=1..4 belongs to the previous windowed scheduler.
    # Resuming it now uses the same uncapped policy as newly confirmed batches.
    # Browser interactions stay serial: submit waits only for its own durable
    # acceptance, never for platform import/proof completion before the next item.
    scope=job.get('dispatch_keys')
    clock_start, monotonic_start=store.clock(),time.monotonic()
    def now():return max(store.clock(),clock_start+(time.monotonic()-monotonic_start))
    def current():return store.get(job['id'])
    def eligible(item):return scope is None or item['key'] in scope
    def update(item,**values):
        basis=item.get('head') or item.get('basis') or {}
        return store.update(job['id'],item_id=item['item_id'],oid=basis.get('oid'),execution_token=runner.execution_token,changes=values)
    def fail(item,error):
        code=getattr(error,'code','BUSINESS_REVIEW')
        update(item,state='PENDING' if code in SESSION_ERRORS else 'REVIEW',error={'code':code,'message':str(error),**({'details':error.details} if getattr(error,'details',None) else {})})
        if code in SESSION_ERRORS:raise error
    def settle(item,record):
        observed=original_observation(item,record)
        if record['state']=='BOUND':
            update(item,state='BOUND',result={'binding':record['binding']},purchase_observation=observed,error=None)
            return True
        if record['state']=='MANUAL_HANDOFF' or record.get('manualHandoff'):
            update(item,state='REVIEW',purchase_observation=observed,error={
                'code':'PURCHASE_MANUAL_HANDOFF','message':'本单转人工处理，按原任务核对已有采购单'})
            return True
        if final:=import_failure(observed):
            error=record.get('error') or {'code':final['errorCode'],'message':import_failure_message(final)}
            update(item,state='REVIEW',purchase_observation=observed,error=error)
            return True
        return False
    def expired(item,record):
        error={'code':'IMPORT_TASK_WAIT_EXPIRED','message':'自动核对窗口已结束，原受理号保留',
               'details':{'taskId':record['taskId'],'pollUntil':item['purchase_poll_until']}}
        update(item,state='REVIEW',purchase_observation=original_observation(item,record),error=error)
    try:
        # Older serial batches may have been interrupted with an item RUNNING.
        # Resolve its durable component identity, rather than marking it done or
        # assuming that the old physical upload never happened.
        for item in current()['items']:
            if eligible(item) and item['state']=='RUNNING':update(item,state='PENDING')
        while True:
            checkpoint(runner.cancel)
            items=[i for i in current()['items'] if eligible(i)]
            ready=[i for i in items if i['state']=='PENDING']
            # Submit the complete selected scope in its original order before
            # observing any import/proof. No local cap on platform-pending jobs.
            for item in ready:
                checkpoint(runner.cancel)
                try:
                    require_orders(runner.business_path,[item]);p=item['package']
                    r=client.request('purchase.status',purchase_id=p['id'])
                    if settle(item,r):continue
                    update(item,purchase_observation=original_observation(item,r))
                    if r['state']=='SUPERSEDED':raise ComponentError('PURCHASE_SUPERSEDED','原任务已重建，请打开后继任务')
                    if r.get('additionalTaskIds'):raise ComponentError('MULTIPLE_IMPORT_TASKS','原导入任务不唯一，请核对原采购记录')
                    if r.get('taskId'):
                        if not r['uploadAttempted']:
                            raise ComponentError('PURCHASE_RECEIPT_CONFLICT','原采购已有受理记录，上传标记不一致，请核对原任务')
                        update(item,state='ACCEPTED',submission_started=True,error=None)
                        continue
                    if r['uploadAttempted']:raise ComponentError('SUBMISSION_UNKNOWN','原上传结果未知，请继续核对原任务')
                    configuration=client.request('configuration');suppliers=Directory(store.folder).suppliers()
                    validate_for_upload(runner.business_path,p,configuration,suppliers,cancel=runner.cancel)
                    update(item,submission_started=True)
                    client.request('purchase.submit',purchase_id=p['id'])
                    r=runner.wait_status('purchase.status','purchase_id',p['id'],cancel_action='purchase.pause',
                        notify=lambda r,item=item:update(item,purchase_observation=original_observation(item,r)))
                    if settle(item,r):continue
                    if r.get('taskId') and r.get('uploadAttempted') and not r.get('additionalTaskIds'):
                        update(item,state='ACCEPTED',purchase_observation=original_observation(item,r),error=None)
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
                    r=client.request('purchase.status',purchase_id=pid)
                    if settle(item,r):continue
                    task_id=r.get('taskId')
                    if not task_id or not r['uploadAttempted'] or r.get('additionalTaskIds'):
                        raise ComponentError('PURCHASE_RECEIPT_CONFLICT','原受理记录待核，未发起上传')
                    deadline=item.get('purchase_poll_until')
                    if deadline is None:
                        deadline=now()+runner.verification_wait
                        update(item,purchase_poll_until=deadline)
                    elif now()>=deadline:
                        expired(item,r)
                        continue
                    item={**item,'purchase_poll_until':deadline,'purchase_observation':original_observation(item,r)}
                    client.request('purchase.start',purchase_id=pid,expected_task_id=task_id)
                    r=runner.wait_status('purchase.status','purchase_id',pid,cancel_action='purchase.pause',
                        notify=lambda r,item=item:update(item,purchase_observation=original_observation(item,r)))
                    if settle(item,r):continue
                    e=r.get('error') or {}
                    if e.get('code')!='TASK_PENDING':
                        raise ComponentError(e.get('code','PURCHASE_NEEDS_REVIEW'),e.get('message','原采购结果待核；其他订单继续'),e.get('details'))
                    if now()>=deadline:expired(item,r)
                    else:runner.progress('平台正在处理原采购任务')
                except SourceDeleted:
                    basis=item.get('head') or item.get('basis') or {}
                    store.forget_missing(job['id'],item['item_id'],basis.get('oid'),runner.execution_token,runner.business_path)
                except Cancelled:raise
                except Exception as exc:fail(item,exc)
            remaining=[i for i in current()['items'] if eligible(i) and i['state'] in ('PENDING','ACCEPTED')]
            if not remaining:break
            if any(i['state']=='PENDING' for i in remaining):continue
            wake_after=min(i['purchase_poll_until'] for i in remaining)-now()
            if runner.cancel.wait(min(runner.verification_interval,max(0,wake_after))):raise Cancelled('已安全暂停，保留全部受理任务；继续时不重传')
    except SourceDeleted:return None
    current_job=current()
    outside=any(i['state'] in ('PENDING','ACCEPTED') and not eligible(i) for i in current_job['items'])
    from aoxiang_workflow_state import summary
    state='PAUSED' if outside else 'COMPLETED_WITH_ERRORS' if any(i['state'] in ('FAILED','REVIEW') for i in current_job['items']) else 'COMPLETED'
    return store.finish_upload(job['id'],runner.execution_token,state,message=summary({**current_job,'state':state}))
