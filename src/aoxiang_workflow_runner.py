"""Serial batch orchestration over the existing component protocol and business services."""
import json
import time
from pathlib import Path

from runtime_support import Cancelled, checkpoint
from aoxiang_client import ComponentError
from aoxiang_directory import Directory, atomic_json
from aoxiang_purchase_tasks import prepare, validate_for_upload
from aoxiang_receipts import synchronize
from aoxiang_workflows import DONE_ITEM

RETRYABLE = {'TASK_PENDING', 'PURCHASE_DOWNLOAD_HTTP', 'DOWNLOAD_FAILED', 'DOWNLOAD_TIMEOUT', 'TASK_NOT_FOUND', 'SAVE_BLOCKED'}
SESSION_ERRORS = {'COMPONENT_EXITED', 'COMPONENT_DISCONNECTED', 'PROTOCOL_FRAME', 'FRAME_TOO_LARGE', 'DISPATCH_UNKNOWN', 'RESPONSE_UNKNOWN', 'WORKER_RESPONSE_UNKNOWN', 'WRONG_IDENTITY', 'CONNECTION_NOT_ESTABLISHED', 'LOGIN_REQUIRED'}


def package_ref(p):
    return {k: p[k] for k in ('id', 'package_file', 'package_sha256', 'plan_file', 'plan_sha256')}


class Runner:
    def __init__(self, store, client, component_root, business_path, *, cancel, progress=lambda _: None, delay=.8,
                 verification_wait=30, verification_interval=3):
        self.store, self.client = store, client
        self.component_root, self.business_path = Path(component_root), Path(business_path)
        self.cancel, self.progress, self.delay = cancel, progress, delay
        self.verification_wait, self.verification_interval = verification_wait, verification_interval

    def wait_status(self, action, id_field, id, *, cancel_action, notify=lambda _: None):
        while True:
            r = self.client.request(action, **{id_field: id})
            notify(r)
            self.progress(r.get('stage') or r.get('state', '等待平台任务'))
            if not r.get('active', r.get('state') == 'RUNNING'):
                return r
            if self.cancel.wait(self.delay):
                self.client.request(cancel_action, **{id_field: id})
                raise Cancelled('已停止新动作；当前任务安全结束后保留原记录')

    def template(self, job):
        if job.get('template_receipt'):
            return job['template_receipt']
        checkpoint(self.cancel)
        r = self.client.request('template.start')
        r = self.wait_status('template.status', 'template_id', r['id'], cancel_action='template.cancel')
        if r['state'] != 'READY':
            error = r.get('error') or {}
            raise ComponentError(error.get('code', 'TEMPLATE_FAILED'), error.get('message', '最新采购模板未验收'))
        self.store.update(job['id'], execution_token=job.get('execution_token'), template_receipt=r['receipt'])
        return r['receipt']

    def prepare_item(self, job, item, configuration, suppliers):
        p = item.get('package')
        if not p:
            template = self.template(self.store.get(job['id']))
            p = prepare(self.business_path, item['head'], configuration, suppliers, template,
                        self.store.folder / 'purchase-packages', self.component_root / 'assets/purchase-import-template-contract.json',
                        cancel=self.cancel, progress=self.progress)
            try:atomic_json(Path(p['package_file']).parent / '本地任务索引.json', p)
            finally:
                from deletion_barrier import package_finished
                package_finished(self.business_path,item['head']['oid'],p['id'])
            summary = {'quantity': p['intent']['totalQuantity'], 'amount': p['intent']['totalAmount'],
                       'sku_count': len(p['intent']['rows']), 'supplier': p['intent']['supplierCode']}
            if p['intent'].get('pricePolicy'):
                summary.update(payment_amount=p['intent']['paymentTotalAmount'], rounding_delta=p['intent']['roundingDelta'])
            # Save exact package identity before the separate component reservation.
            self.store.update(job['id'], item_id=item['item_id'], oid=(item.get('head') or item.get('basis') or {}).get('oid'), execution_token=job.get('execution_token'), changes={'package': package_ref(p), 'summary': summary})
        checkpoint(self.cancel)
        from aoxiang_order_guard import require_orders
        require_orders(self.business_path,[item])
        accepted = self.client.request('purchase.reserve', package_file=p['package_file'], package_sha256=p['package_sha256'])
        if accepted['record']['uploadAttempted']:
            raise ValueError('此预览已进入上传；请核对原采购任务，不重复列为待上传')
        return 'PREPARED', {'purchase_id': p['id']}

    def upload_item(self, item, configuration, suppliers, notify=lambda _: None):
        p = item['package']
        record = self.client.request('purchase.status', purchase_id=p['id'])
        notify(record)
        if record['state'] == 'BOUND':
            return 'BOUND', {'binding': record['binding']}
        if record.get('additionalTaskIds'):
            raise ComponentError('MULTIPLE_IMPORT_TASKS', '同一采购存在多个受理任务号，请在原采购记录核对')
        if record['uploadAttempted'] and not record.get('taskId'):
            raise ComponentError('SUBMISSION_UNKNOWN', '上传已发起，但尚未取得唯一受理回执；请核对已有平台采购单')
        if not record['uploadAttempted']:
            from aoxiang_order_guard import require_orders
            require_orders(self.business_path,[item])
            validate_for_upload(self.business_path, p, configuration, suppliers, cancel=self.cancel)
        deadline = time.monotonic() + self.verification_wait
        while True:
            checkpoint(self.cancel)
            self.client.request('purchase.start', purchase_id=p['id'])
            record = self.wait_status('purchase.status', 'purchase_id', p['id'], cancel_action='purchase.pause', notify=notify)
            if record['state'] == 'BOUND':
                return 'BOUND', {'binding': record['binding']} | ({'uploadWarning': record['uploadWarning']} if record.get('uploadWarning') else {})
            error = record.get('error') or {}
            # Only poll a known accepted task. start() then takes the component's
            # observe-only branch; the durable upload flag is never reset.
            remaining = deadline - time.monotonic()
            if (record.get('taskId') and record.get('uploadAttempted') and not record.get('additionalTaskIds')
                    and error.get('code') == 'TASK_PENDING' and remaining > 0):
                self.progress('翱象已受理，等待原采购任务完成；正在继续核对…')
                if self.cancel.wait(min(self.verification_interval, remaining)):
                    raise Cancelled('已停止等待；原受理任务保留，可继续核对采购结果')
                continue
            raise ComponentError(error.get('code', 'PURCHASE_NEEDS_REVIEW'), error.get('message', '原采购任务尚未核实成功，不能重传'), error.get('details'))

    def receipt_item(self, item, configuration, suppliers):
        from aoxiang_receipt_scope import require,formal_receipt_exists
        initial=require(self.business_path,[item])[item['key']]
        guard_item={**item,'basis':{**item['basis'],**initial['head']}}
        id = item['purchase_id']
        def observe(r):
            if r['scope']['orderId'] != item['order_id'] or r['basis'] != item['basis']:
                raise ValueError('收货任务关联变化，停止采用')
            from aoxiang_receipt_scope import assess
            current=assess(self.business_path,[guard_item])[item['key']]
            if not current['eligible']:
                self.client.request('receipt.pause',purchase_id=id)
                raise ComponentError('RECEIPT_SOURCE_RETIRED',current['reason']+'；已请求安全停止，原受理任务及文件保留')
            self.store.observe_schedule(id, r)
        record = self.client.request('receipt.status', purchase_id=id)
        observe(record)
        if record['state'] not in ('READY', 'SYNCED', 'VOIDED', 'VOIDED_REVIEW'):
            checkpoint(self.cancel)
            require(self.business_path,[item])
            self.client.request('receipt.start', purchase_id=id)
            record = self.wait_status('receipt.status', 'purchase_id', id, cancel_action='receipt.pause', notify=observe)
        if record['state']=='VOIDED' and formal_receipt_exists(self.business_path,item['basis']):
            record=self.client.request('receipt.host_void_conflict',purchase_id=id,reason='本机已有正式收货记录，平台现已作废；保留原实收，需人工核对后续处理')
            observe(record)
        if record['state']=='VOIDED':return 'VOIDED',{'summary':record.get('summary')}
        if record['state']=='VOIDED_REVIEW':
            issues=(record.get('summary') or {}).get('issues') or []
            raise ComponentError('RECEIPT_VOID_REVIEW',record.get('hostConflict') or '；'.join(i['message'] for i in issues) or '平台已作废，但存在实收或状态冲突；原事实保留，请核对处理')
        if record['state'] == 'READY':
            index = self.store.folder / 'purchase-packages' / id / '本地任务索引.json'
            if not index.is_file():
                raise ValueError('原采购付款依据未找到，最终实收保留待核')
            package = json.loads(index.read_text(encoding='utf-8'))
            receipt = synchronize(self.business_path, package, record['manifest'], configuration, suppliers, cancel=self.cancel, progress=self.progress)
            record = self.client.request('receipt.synced', purchase_id=id, manifest_sha256=record['manifest']['sha256'], receipt=receipt)
            observe(record)
        if record['state'] == 'SYNCED':
            return 'SYNCED', {'receipt': record.get('hostReceipt'), 'summary': record.get('summary')}
        if record['state'] == 'IDLE':
            return 'OBSERVED', {'summary': record.get('summary')}
        error = record.get('error') or {}
        raise ComponentError(error.get('code', 'RECEIPT_NEEDS_REVIEW'), error.get('message', '实收字段或主子状态未通过；保留原件待核'))

    def run(self, id):
        job = self.store.claim(id)
        self.execution_token=job['execution_token']
        from deletion_barrier import SourceDeleted
        for identity in [i['item_id'] for i in job['items']]:
            try:current=self.store.get(id)
            except SourceDeleted:return None
            item=next((i for i in current['items'] if i['item_id']==identity),None)
            if item is None:continue
            def update(_identity=identity,_oid=(item.get('head') or item.get('basis') or {}).get('oid'),**kw):
                return self.store.update(id,item_id=_identity,oid=_oid,execution_token=self.execution_token,**kw)
            try:
                if item['state'] in DONE_ITEM:
                    continue
                scope=self.store.get(id).get('dispatch_keys')
                if scope is not None and item['key'] not in scope:continue
                checkpoint(self.cancel)
                position = item['position']
                if job.get('automatic'):
                    schedule = self.store.schedule(item['purchase_id'])
                    if not schedule or schedule.get('automation_paused'):
                        update(changes={'state': 'SKIPPED', 'error': None})
                        continue
                from aoxiang_order_guard import deleted_orders
                if job['kind']=='receipt':
                    from aoxiang_receipt_scope import assess
                    eligibility=assess(self.business_path,[item])[item['key']]
                    if not eligibility['eligible']:
                        self.store.observe_schedule(item['purchase_id'],None,error={'code':'RECEIPT_SOURCE_RETIRED','message':eligibility['reason']},review=True)
                        update(changes={'state':'SKIPPED','source_retired':True,'error':{'code':'RECEIPT_SOURCE_RETIRED','message':eligibility['reason']}})
                        continue
                missing=deleted_orders(self.business_path,[item]) if job['kind']!='receipt' else {}
                if missing:
                    basis=item.get('head') or item.get('basis') or {}
                    if not self.store.forget_missing(id,identity,basis.get('oid'),self.execution_token,self.business_path):
                        update(changes={'state':'SKIPPED','error':{'code':'SOURCE_ORDER_DELETED','message':'来源不可用，未派发平台动作'}})
                    continue
                # Read again at each dispatch: a batch never silently adopts stale host mappings.
                configuration = self.client.request('configuration')
                suppliers = Directory(self.store.folder).suppliers()
                basis = item.get('head') or item.get('basis') or {}
                text = f"{position+1}/{len(job['items'])} · {item.get('display_name') or basis.get('name') or basis.get('number')} · {basis.get('warehouse', '')}"
                self.progress(text)
                update(changes={'state': 'RUNNING', 'error': None}, message=text)
                try:
                    if job['kind'] == 'prepare':
                        state, result = self.prepare_item(job, item, configuration, suppliers)
                    elif job['kind'] == 'upload':
                        from aoxiang_purchase_messages import observation
                        state, result = self.upload_item(item, configuration, suppliers,
                            notify=lambda r,update_item=update: update_item(changes={'purchase_observation': observation(r)}))
                    else:
                        state, result = self.receipt_item(item, configuration, suppliers)
                    update(changes={'state': state, 'result': result, 'error': None})
                except SourceDeleted:
                    continue
                except Cancelled:
                    update(changes={'state': 'PENDING'})
                    raise
                except Exception as exc:
                    code = getattr(exc, 'code', 'BUSINESS_REVIEW')
                    error = {'code': code, 'message': str(exc)}
                    if getattr(exc, 'details', None):error['details'] = exc.details
                    if job['kind'] == 'receipt':
                        self.store.observe_schedule(item['purchase_id'], None, error=error, review=code not in RETRYABLE)
                    if code in SESSION_ERRORS:
                        update(changes={'state': 'PENDING', 'error': error})
                        raise
                    update(changes={'state': 'FAILED' if code in RETRYABLE else 'REVIEW', 'error': error})
            except SourceDeleted:
                continue
        try:current = self.store.get(id)
        except SourceDeleted:return None
        if any(i['state']=='PENDING' for i in current['items']):
            scope=current.get('dispatch_keys')
            if scope is not None and not any(i['state']=='PENDING' and i['key'] in scope for i in current['items']):
                return self.store.update(id,execution_token=self.execution_token,state='PAUSED',message='勾选范围已处理；未勾选项保留，等待另行选择，不自动执行')
            return self.store.update(id,execution_token=self.execution_token,state='QUEUED',message='继续处理本批执行范围中新排入的失败重跑项')
        bad = sum(i['state'] in ('FAILED', 'REVIEW') for i in current['items'])
        from aoxiang_workflow_state import summary
        final = {**current, 'state': 'COMPLETED_WITH_ERRORS' if bad else 'READY' if job['kind'] == 'prepare' else 'COMPLETED'}
        return self.store.update(id, execution_token=self.execution_token, state=final['state'], message=summary(final))
