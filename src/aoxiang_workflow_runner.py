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
    def __init__(self, store, client, component_root, business_path, *, cancel, progress=lambda _: None, delay=.8):
        self.store, self.client = store, client
        self.component_root, self.business_path = Path(component_root), Path(business_path)
        self.cancel, self.progress, self.delay = cancel, progress, delay

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
        self.store.update(job['id'], template_receipt=r['receipt'])
        return r['receipt']

    def prepare_item(self, job, item, configuration, suppliers):
        p = item.get('package')
        if not p:
            template = self.template(self.store.get(job['id']))
            p = prepare(self.business_path, item['head'], configuration, suppliers, template,
                        self.store.folder / 'purchase-packages', self.component_root / 'assets/purchase-import-template-contract.json',
                        cancel=self.cancel, progress=self.progress)
            atomic_json(Path(p['package_file']).parent / '本地任务索引.json', p)
            summary = {'quantity': p['intent']['totalQuantity'], 'amount': p['intent']['totalAmount'],
                       'sku_count': len(p['intent']['rows']), 'supplier': p['intent']['supplierCode']}
            # Save exact package identity before the separate component reservation.
            self.store.update(job['id'], position=item['position'], changes={'package': package_ref(p), 'summary': summary})
        checkpoint(self.cancel)
        accepted = self.client.request('purchase.reserve', package_file=p['package_file'], package_sha256=p['package_sha256'])
        if accepted['record']['uploadAttempted']:
            raise ValueError('此预览已进入上传；请核对原采购任务，不重复列为待上传')
        return 'PREPARED', {'purchase_id': p['id']}

    def upload_item(self, item, configuration, suppliers):
        p = item['package']
        record = self.client.request('purchase.status', purchase_id=p['id'])
        if record['state'] == 'BOUND':
            return 'BOUND', {'binding': record['binding']}
        if record['uploadAttempted'] and not record.get('receipt'):
            raise ComponentError('SUBMISSION_UNKNOWN','原上传结果尚未核实，没有唯一受理任务号。请在原采购页核对关联；禁止重新上传。')
        if not record['uploadAttempted']:
            validate_for_upload(self.business_path, p, configuration, suppliers, cancel=self.cancel)
        checkpoint(self.cancel)
        self.client.request('purchase.start', purchase_id=p['id'])
        record = self.wait_status('purchase.status', 'purchase_id', p['id'], cancel_action='purchase.pause')
        if record['state'] != 'BOUND':
            error = record.get('error') or {}
            raise ComponentError(error.get('code', 'PURCHASE_NEEDS_REVIEW'), error.get('message', '原采购任务尚未核实成功，不能重传'))
        return 'BOUND', {'binding': record['binding']}

    def receipt_item(self, item, configuration, suppliers):
        id = item['purchase_id']
        def observe(r):
            if r['scope']['orderId'] != item['order_id'] or r['basis'] != item['basis']:
                raise ValueError('收货任务关联变化，停止采用')
            self.store.observe_schedule(id, r)
        record = self.client.request('receipt.status', purchase_id=id)
        observe(record)
        if record['state'] not in ('READY', 'SYNCED'):
            checkpoint(self.cancel)
            self.client.request('receipt.start', purchase_id=id)
            record = self.wait_status('receipt.status', 'purchase_id', id, cancel_action='receipt.pause', notify=observe)
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
        job = self.store.update(id, state='RUNNING', message='读取本地配置及原任务状态')
        configuration = self.client.request('configuration')
        suppliers = Directory(self.store.folder).suppliers()
        if job['kind'] == 'prepare' and any(i['state'] not in DONE_ITEM and not i.get('package') for i in job['items']):
            self.template(job)
        for position in range(len(job['items'])):
            item=self.store.get(id)['items'][position]
            if item['state'] in DONE_ITEM:
                continue
            checkpoint(self.cancel)
            position = item['position']
            if job.get('automatic'):
                schedule = self.store.schedule(item['purchase_id'])
                if not schedule or schedule.get('automation_paused'):
                    self.store.update(id, position=position, changes={'state': 'SKIPPED', 'error': None})
                    continue
            # Read again at each dispatch: a batch never silently adopts stale host mappings.
            configuration = self.client.request('configuration')
            suppliers = Directory(self.store.folder).suppliers()
            basis = item.get('head') or item.get('basis') or {}
            text = f"{position+1}/{len(job['items'])} · {item.get('display_name') or basis.get('name') or basis.get('number')} · {basis.get('warehouse', '')}"
            self.progress(text)
            self.store.update(id, position=position, changes={'state': 'RUNNING', 'error': None}, message=text)
            try:
                if job['kind'] == 'prepare':
                    state, result = self.prepare_item(job, item, configuration, suppliers)
                elif job['kind'] == 'upload':
                    state, result = self.upload_item(item, configuration, suppliers)
                else:
                    state, result = self.receipt_item(item, configuration, suppliers)
                self.store.update(id, position=position, changes={'state': state, 'result': result, 'error': None})
            except Cancelled:
                self.store.update(id, position=position, changes={'state': 'PENDING'})
                raise
            except Exception as exc:
                code = getattr(exc, 'code', 'BUSINESS_REVIEW')
                error = {'code': code, 'message': str(exc)}
                if job['kind'] == 'receipt':
                    self.store.observe_schedule(item['purchase_id'], None, error=error, review=code not in RETRYABLE)
                if code in SESSION_ERRORS:
                    self.store.update(id, position=position, changes={'state': 'PENDING', 'error': error})
                    raise
                self.store.update(id, position=position, changes={'state': 'FAILED' if code in RETRYABLE else 'REVIEW', 'error': error})
        current = self.store.get(id)
        if any(i['state']=='PENDING' for i in current['items']):
            return self.store.update(id,state='QUEUED',message='继续处理本批中新排入的失败重跑项')
        bad = sum(i['state'] in ('FAILED', 'REVIEW') for i in current['items'])
        return self.store.update(id, state='COMPLETED_WITH_ERRORS' if bad else 'READY' if job['kind'] == 'prepare' else 'COMPLETED', message=f'已处理 {len(job["items"])} 单；待核/失败 {bad} 单')
