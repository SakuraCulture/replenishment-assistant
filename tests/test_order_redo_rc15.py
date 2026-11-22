"""Regression coverage for the rc14 outcomes on two warehouses of one store."""
from contextlib import closing
from decimal import Decimal
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
from unittest import TestCase
from unittest.mock import patch

import openpyxl

from aoxiang_client import ComponentError
from aoxiang_payment_preflight import preflight_payment
from aoxiang_preview_scope import inspect_scope
from aoxiang_purchase import freeze_payment
from aoxiang_purchase_tasks import validate_for_upload
from aoxiang_receipt_scope import assess, review_stamp
from aoxiang_receipts import synchronize
from aoxiang_workflows import Workflows
import daily_batch
import jst_auto_order_store as orders
import jst_cancellation as cancellation
import lifecycle_io as io
import lifecycle_store
import order_lifecycle as lifecycle
import purchases
from test_aoxiang_purchase import config
from test_order_redo_p1 import AX, Dual, JST, QUANTITY, STOCK, SUPPLIERS, WAREHOUSES, evidence
import test_order_redo_followup as fixtures
import work_repository as wr


PRICE = {'河北仓': 40, '山东仓': 48}


def old_copy(root, source, wh):
    """Copy a seed created by rc13, keeping the source and its files read-only."""
    old = Path(source)/wh
    data = json.loads((old/'rc13-history.json').read_text('utf-8'))
    settings = root/'settings'
    settings.mkdir()
    with closing(wr.read_connection(old/'settings/workbench.sqlite3')) as db, \
         closing(sqlite3.connect(settings/'workbench.sqlite3')) as copy:
        db.backup(copy)
    shutil.copytree(old/'settings/warehouse_catalogs', settings/'warehouse_catalogs')
    shutil.copytree(old/'settings/aoxiang', settings/'aoxiang')
    case = Dual.__new__(Dual)
    case.root, case.work = root, wr.Repository(settings)
    case.path = case.work.path
    case.config = config()
    case.heads = {h['warehouse']: h for h in purchases.targets(case.path)}
    case.workflow = Workflows(settings/'aoxiang', business_path=case.path)
    case.jst, case.ax = JST(), AX(case)
    case.jst.orders = data['orders']
    case.sequence = 0
    case.ax.register(data['preview'])
    return case, data


def corrected_sheet(path, head, document_number, quantity, amount, *, zero=False):
    fields = io.fields('payment')
    book = openpyxl.Workbook()
    book.active.append(fields)
    base = {'补货单号':head['number'], '签发修订':head['revision'], '门店编码':head['store'],
            '供货仓':head['warehouse'], '商品名称':'合成商品', '数量单位':'库存单位',
            io.DOCNUM['payment']:document_number, io.DOCDATE['payment']:head['day'], '币种':'CNY'}
    rows = [('00000', quantity, amount)]
    if zero:
        rows.append(('00001', 0, 0))
    for sku, qty, value in rows:
        row = {**base, '商品编码':sku, io.QUANTITIES['payment']:qty, '实际金额':value}
        book.active.append([row.get(k) for k in fields])
    book.save(path)
    book.close()


def correction(case, h, *, zero=False):
    old = lifecycle.history(case.path, h, 'payment')[0]
    doc_number = old['payload']['rows'][0]['document_number']
    path = case.root/'corrected-payment.xlsx'
    corrected_sheet(path, h, doc_number, QUANTITY[h['warehouse']], PRICE[h['warehouse']]-1, zero=zero)
    file, = io.inspect([path])
    spec, = file['sheets']
    scan = io.scan('payment', [{**file, **spec, 'mapping':{k:i for i,k in enumerate(spec['headers'])}}])
    saved, = lifecycle.commit(case.path,lifecycle.prepare_numbered(case.path,scan,h))
    return saved


def observe_purchase(case, wh, *, complete):
    package = case.make_package(wh)
    upload = case.runner().run(case.queue(wh, package)['id'])['items'][0]
    assert upload['state'] == 'BOUND', upload
    record, proof = case.stage_receipt(package)
    finished = case.ax.observations[package['id']]
    initial = finished if complete else {
        **finished, 'state':'IDLE', 'terminal':False, 'terminalKind':None,
        'summary':{'status':'采购中'}, 'manifest':None,
    }
    case.ax.observations[package['id']] = initial
    case.workflow.configure_schedules([record], 5, True)
    job = case.workflow.receipt([record])
    case.workflow.observe_schedule(package['id'],initial)
    return package, record, proof, finished, job


def real_dispatch(case, package, *, consume=True):
    component = os.environ.get('REPLENISHMENT_TEST_AX_COMPONENT')
    if not component:
        return case.ax.guard(package, consume=consume)
    script = """
import fs from 'node:fs';import {checkHostDispatch} from './src/purchase-host-guard.mjs';
const intent=JSON.parse(fs.readFileSync(process.argv[1],'utf8'));
try{checkHostDispatch(intent,{consume:process.argv[2]==='1',requestHash:'a'.repeat(64)});console.log(JSON.stringify({ok:true}));}
catch(e){console.log(JSON.stringify({ok:false,code:e.code,message:e.message}));}
"""
    result = subprocess.run([str(Path(component)/'runtime/node.exe'), '--input-type=module', '-e', script,
                             package['package_file'],str(int(consume))], cwd=component,
                            env={**os.environ, 'REPLENISHMENT_BUSINESS_DB':str(case.path)}, check=True,
                            capture_output=True, text=True, encoding='utf-8', timeout=15)
    return json.loads(result.stdout.splitlines()[-1])


class Regressions(TestCase):
    dual = fixtures.FollowUp.dual
    paid = fixtures.FollowUp.paid
    native_binding = fixtures.FollowUp.native_binding
    reopen = fixtures.FollowUp.reopen
    fail_lookup = fixtures.FollowUp.fail_lookup

    def test_upgrade_recovers_cancellation_overwritten_in_rc13(self):
        seed = os.environ.get('REPLENISHMENT_RC13_CANCEL_FIXTURES')
        if not seed:
            self.skipTest('Uses the versioned rc13 upgrade fixtures created by seed_rc13.py')
        for wh in WAREHOUSES:
            for decision in ('reuse', 'correct', 'stop'):
                with self.subTest(warehouse=wh, decision=decision):
                    root = Path(self.enterContext(tempfile.TemporaryDirectory()))
                    case, data = old_copy(root, seed, wh)
                    head, online = case.heads[wh], data['online'][wh]
                    other = next(w for w in WAREHOUSES if w != wh)
                    gid = data['gids'][wh]
                    old = lifecycle.history(case.path,head,'payment')
                    frozen = data['preview']
                    # Audit first so the upload regression also records what the
                    # frozen program actually allowed before asserting the gate.
                    waiting = daily_batch.read(case.path,root/'stage.sqlite',{
                        'stage_view':'payment','stage_filter':'待处理','all_matched':True})['records']
                    state = inspect_scope(case.path,root/'empty-original',[head,case.heads[other]])
                    original_guard = real_dispatch(case,frozen,consume=False)
                    queued = case.runner().run(data['queued'])['items'][0]
                    evidence({'scenario':'upgrade','warehouse':wh,'decision':decision,'warehouse_order':head['number'],
                              'online':online,'queued':queued['state'],'uploads':len(case.ax.uploads),
                              'node':original_guard,'shown_waiting':[r['number'] for r in waiting]})
                    self.assertIn(head['number'],[r['number'] for r in waiting])
                    self.assertEqual([r['state'] for r in state],['blocked','ready'])
                    self.assertEqual((original_guard.get('code'),queued['error']['code']),
                                     ('PURCHASE_BASIS_CHANGED','PAYMENT_CANCEL_REVIEW'))
                    self.assertFalse(case.ax.uploads)
                    with self.assertRaisesRegex(ValueError,'付款待重新核对'):
                        validate_for_upload(case.path,frozen,case.config,SUPPLIERS)
                    for failure in ('timeout','identity','missing'):
                        with self.fail_lookup(case,failure,online):
                            case.recover(online)
                        self.reopen(case)
                        with self.assertRaisesRegex(ValueError,'付款待重新核对'):
                            preflight_payment(case.path,head)
                        with self.assertRaisesRegex(ValueError,'已取消'):
                            cancellation.begin_redo(case.path,online)
                        self.assertEqual(case.remaining(gid), STOCK[wh]-QUANTITY[wh])
                    self.assertEqual(case.cancel(online)['stage'], 'CANCELLED')
                    with self.assertRaisesRegex(ValueError,'付款待重新核对'):
                        freeze_payment(case.path,head,case.config,SUPPLIERS)
                    if decision == 'correct':
                        self.assertEqual(correction(case,head)['version'],2)
                    approved = cancellation.save_review(case.path,head,self.case_stamp(case,wh),decision,
                                                          '隔离核对员','按原单和付款凭据核对')
                    self.assertEqual(real_dispatch(case,frozen).get('code'),'PURCHASE_BASIS_CHANGED')
                    if decision == 'stop':
                        with self.assertRaisesRegex(ValueError, '终止本轮采购'):
                            preflight_payment(case.path,head)
                    else:
                        self.assertEqual(preflight_payment(case.path,head)['payment']['version'],approved['payment_version'])
                    self.assertEqual(lifecycle.history(case.path,head,'payment')[-1],old[0])
                    with closing(wr.read_connection(case.path)) as db:
                        for original in old[0]['payload']['files']:
                            self.assertIsNotNone(lifecycle_store.file_row(db,original['sha']))
                    self.assertEqual(preflight_payment(case.path,case.heads[other])['payment']['version'],1)

    def case_stamp(self, case, wh):
        with closing(wr.read_connection(case.path)) as db:
            return cancellation.review_context(db,case.heads[wh])['stamp']

    def test_explicit_zero_amount_change_keeps_original_receipt(self):
        for wh in WAREHOUSES:
            for complete in (False,True):
                for zero in (False,True):
                    with self.subTest(warehouse=wh, complete=complete, explicit_zero=zero):
                        case = self.dual()
                        self.paid(case)
                        h = case.heads[wh]
                        package, record, proof, finished, job = observe_purchase(case,wh,complete=complete)
                        pid, order_id = package['id'], record['binding']['orderId']
                        self.native_binding(case,package)
                        zero_version = correction(case,h,zero=zero)
                        self.assertEqual(zero_version['version'],2)
                        case.workflow.retire_receipt_sources()
                        self.reopen(case)
                        schedule = case.workflow.schedule(pid)
                        status = case.workflow.get(job['id'])['items'][0]['state']
                        evidence({'scenario':'amount','warehouse':wh,'complete':complete,'zero':zero,
                                  'purchase':order_id,'task':status,'retired':schedule.get('retired'),
                                  'final_pending':schedule['final_pending']})
                        self.assertFalse(schedule.get('retired'))
                        self.assertEqual((schedule['order_id'],schedule['interval_seconds'],schedule['final_pending']),
                                         (order_id,300,complete))
                        self.assertEqual(schedule['enabled'],not complete)
                        self.assertNotEqual(status,'SKIPPED')
                        self.assertFalse(lifecycle.history(case.path,h,'receipt'))
                        display = daily_batch.read(case.path,case.root/'stage.sqlite',
                                                  {'stage_view':'receipt','stage_filter':'待处理','platform_tracking':True})
                        source, = [r['platform_purchase'] for r in display['records'] if r['number']==h['number']]
                        self.assertEqual(source['orderId'],order_id)
                        case.ax.observations[pid] = finished
                        blocked = case.runner().run(job['id'])['items'][0]
                        self.assertEqual(blocked['error']['code'],'RECEIPT_PAYMENT_REVIEW')
                        change = assess(case.path,[{'key':pid,'basis':record['basis']}])[pid]['payment_review']
                        self.assertEqual(Decimal(change['difference']),-1)
                        self.assertEqual(len(change['rows']),1)
                        approval = {'stamp':review_stamp(change,pid,order_id,proof['sha256']),
                                    'reviewer':'隔离收货员','basis':'核对金额及原采购'}
                        receipt = synchronize(case.path,package,proof,case.config,SUPPLIERS,approval=approval)
                        synced = case.ax.request('receipt.synced',purchase_id=pid,receipt=receipt,manifest_sha256=proof['sha256'])
                        case.workflow.observe_schedule(pid,synced)
                        self.assertEqual(case.workflow.get(job['id'])['items'][0]['state'],'SYNCED')
                        self.assertEqual(len(lifecycle.history(case.path,h,'receipt')),1)
                        self.assertEqual(case.ax.uploads,[pid])

    def test_posted_receipt_after_payment_correction_closes_same_task(self):
        for wh in WAREHOUSES:
            for fault in ('before-delivery','after-delivery'):
                with self.subTest(warehouse=wh, fault=fault):
                    case = self.dual()
                    self.paid(case)
                    h = case.heads[wh]
                    package, record, proof, _, job = observe_purchase(case,wh,complete=True)
                    pid = package['id']
                    self.native_binding(case,package)
                    calls = []
                    receive = case.ax.request
                    def interrupted(action, **kwargs):
                        if action == 'receipt.synced':
                            calls.append(action)
                            if fault == 'after-delivery':
                                receive(action, **kwargs)
                            raise ComponentError('QA_REPLY_INTERRUPTED','验收：提交后的组件应答中断')
                        return receive(action, **kwargs)
                    with patch.object(case.ax,'request',side_effect=interrupted):
                        pending = case.runner().run(job['id'])
                    self.assertEqual(pending['items'][0]['state'],'REVIEW')
                    saved, = lifecycle.history(case.path,h,'receipt')
                    self.assertEqual(saved['version'],1)
                    self.assertEqual(correction(case,h)['version'],2)
                    self.reopen(case)
                    case.workflow.retire_receipt_sources()
                    actual = []
                    def restored(action, **kwargs):
                        if action == 'receipt.synced':
                            actual.append(kwargs)
                        return receive(action, **kwargs)
                    with patch.object(case.ax,'request',side_effect=restored):
                        for _ in range(2):
                            pending = case.workflow.get(job['id'])
                            if pending['items'][0]['state'] != 'SYNCED':
                                case.workflow.retry_items(job['id'],[0],pending['sequence'])
                                case.runner().run(job['id'])
                            else:
                                state, _ = case.runner().receipt_item(pending['items'][0],case.config,SUPPLIERS)
                                self.assertEqual(state,'SYNCED')
                    state = case.workflow.get(job['id'])['items'][0]['state']
                    schedule = case.workflow.schedule(pid)
                    evidence({'scenario':'ack','warehouse':wh,'fault':fault,'purchase':record['binding']['orderId'],
                              'task':state,'component':case.ax.observations[pid]['state'],
                              'final_pending':schedule['final_pending'],'receipt_versions':len(lifecycle.history(case.path,h,'receipt'))})
                    self.assertEqual((state,schedule['last_state'],schedule['final_pending']),('SYNCED','SYNCED',False))
                    self.assertEqual(len(actual), int(fault=='before-delivery'))
                    self.assertEqual(lifecycle.history(case.path,h,'receipt'),[saved])
                    self.assertEqual(case.ax.uploads,[pid])

    def test_unmatched_observation_or_ack_keeps_posted_receipt_pending(self):
        for wh in WAREHOUSES:
            for fault in ('other-warehouse','other-po','other-observation','other-version','missing-confirmation'):
                with self.subTest(warehouse=wh,fault=fault):
                    case = self.dual()
                    self.paid(case)
                    h = case.heads[wh]
                    package, record, proof, finished, job = observe_purchase(case,wh,complete=True)
                    pid = package['id']
                    posted = synchronize(case.path,package,proof,case.config,SUPPLIERS)
                    saved, = lifecycle.history(case.path,h,'receipt')
                    self.assertEqual(correction(case,h)['version'],2)
                    candidate = {**finished}
                    if fault == 'other-warehouse':
                        candidate['basis']={**finished['basis'],'warehouse':next(w for w in WAREHOUSES if w!=wh)}
                    elif fault == 'other-po':
                        candidate['scope']={**finished['scope'],'orderId':'PO-OTHER'}
                    elif fault == 'other-observation':
                        candidate['manifest']={**proof,'sha256':'f'*64}
                    elif fault=='other-version':
                        candidate.update(state='SYNCED',hostReceipt={**posted,'version':2})
                    else:
                        candidate.update(state='SYNCED')
                    case.ax.observations[pid]=candidate
                    result=case.runner().run(job['id'])['items'][0]
                    self.assertEqual(result['state'],'REVIEW',result)
                    self.assertTrue(case.workflow.schedule(pid)['final_pending'])
                    self.assertEqual(lifecycle.history(case.path,h,'receipt'),[saved])
                    self.assertEqual(case.ax.uploads,[pid])

    def test_rc14_retired_receipts_reopen_without_unpausing_or_reviving_closed_tasks(self):
        seed = os.environ.get('REPLENISHMENT_RC14_RECEIPT_FIXTURES')
        if not seed:
            self.skipTest('Uses the frozen rc14 source-retirement fixtures created by seed_rc14_retired.py')
        for wh in WAREHOUSES:
            for stage in ('pending','completed'):
                for stop in ('none','voided','synced','manual','wrong-order'):
                    with self.subTest(warehouse=wh,stage=stage,stop=stop):
                        root=Path(self.enterContext(tempfile.TemporaryDirectory()))
                        origin=Path(seed)/(wh+'-'+stage)
                        fixture=json.loads((origin/'retired-history.json').read_text('utf-8'))
                        shutil.copytree(origin/'settings',root/'settings')
                        business=root/'settings/workbench.sqlite3'
                        store=Workflows(root/'settings/aoxiang',business_path=business)
                        pid=fixture['order']['id']
                        id=fixture['job']
                        self.assertTrue(store.schedule(pid)['retired'])
                        self.assertEqual(store.get(id)['items'][0]['state'],'SKIPPED')
                        if stop in ('voided','synced','wrong-order'):
                            with closing(sqlite3.connect(root/'settings/aoxiang/component-state/state.sqlite')) as db, db:
                                observation=fixture['current']
                                if stop=='wrong-order':
                                    observation['scope']['orderId']='NOT-ORIGINAL'
                                else:
                                    observation['state']=stop.upper()
                                db.execute('UPDATE purchase_receipts SET payload=? WHERE id=?',(json.dumps(observation),pid))
                        if stop=='manual':
                            with store.db(True) as db:
                                job=store._get(db,id)
                                job['items'][0]['cancelled_unstarted']=True
                                store._save(db,job,'CANCELLED_REMAINDER_ONLY')
                        store.retire_receipt_sources()
                        store=Workflows(store.folder,business_path=business)
                        schedule=store.schedule(pid)
                        self.assertTrue(schedule['automation_paused'])
                        self.assertFalse(schedule['enabled'])
                        self.assertEqual(schedule['interval_seconds'],300)
                        self.assertEqual(schedule['order_id'],fixture['order']['binding']['orderId'])
                        restored=stop in ('none','manual')
                        self.assertEqual(bool(schedule['retired']),not restored)
                        self.assertEqual(schedule['final_pending'],restored and stage=='completed')
                        self.assertEqual(store.get(id)['items'][0]['state'],'REVIEW' if stop=='none' else 'SKIPPED')
