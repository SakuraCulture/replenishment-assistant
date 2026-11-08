"""Portable host -> real Node protocol tests. No browser binding or platform connection."""
import copy
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src')]
import test_lifecycle as fixture
from aoxiang_client import ProcessClient, ComponentError
from aoxiang_directory import file_hash, atomic_json
from aoxiang_purchase_tasks import prepare, validate_for_upload

COMPONENT = ROOT / 'components/aoxiang'
NODE = COMPONENT / 'runtime/node.exe'
CONTRACT = COMPONENT / 'assets/purchase-import-template-contract.json'


class PurchaseTaskTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.LifecycleTests()
        self.f.setUp()
        self.f.save('return')
        self.f.save('payment', [{'付款单商品数量': 7, '实际金额': .99}])
        # New procurement requires a complete current label source. This fixture
        # explicitly declares a non-medical item; existing quantity/order assertions stay.
        from test_jst import catalog
        from warehouse_catalog import Repository
        Repository(self.f.path.parent).save(catalog())
        self.root = self.f.root / 'aoxiang'
        self.suppliers = {'河北仓': '2078246', '山东仓': 'ZXC2025'}
        script = """import {StateStore} from './src/state.mjs';import {freshConfig,saveConfig} from './src/config.mjs';
const root=process.argv[1],s=await StateStore.open(root);await saveConfig(root,freshConfig({accountId:'A',merchantCode:'M',displayName:'离线合成商家'},[{buyerWarehouseCode:'T',displayName:'测试店',operatingStores:[],mappingSource:'offline-fixture',mappingVerifiedAt:'2026-11-02T00:00:00Z'}]));s.close();"""
        self.node(script, str(self.root / 'component'))
        self.client = ProcessClient(COMPONENT, self.root / 'component')
        self.client.start()
        self.config = self.client.request('configuration')
        template = COMPONENT / 'test/fixtures/purchase-import-template.xlsx'
        script = "import {inspectPurchaseTemplate,templateContract} from './src/purchase-template.mjs';console.log(JSON.stringify({...await inspectPurchaseTemplate(process.argv[1]),contractVersion:(await templateContract()).version}));"
        receipt = json.loads(self.node(script, str(template)))
        receipt.update(status='SUPPORTED', file=str(template), identity={'accountId': 'A', 'merchantCode': 'M'},
                       recordFile=str(self.root / 'template-record.json'), latestFile=str(self.root / 'template-latest.json'),
                       attemptId='offline-fixture', importSubmissions=0, uiProductLimit=None,
                       source='isolated offline fixture, NOT a live template receipt')
        self.receipt = receipt
        atomic_json(receipt['recordFile'], receipt)
        atomic_json(receipt['latestFile'], receipt)

    def tearDown(self):
        self.assertTrue(self.client.close(timeout=10))
        self.f.tearDown()

    def node(self, script, *args):
        r = subprocess.run([str(NODE), '--input-type=module', '-e', script, *args], cwd=COMPONENT, capture_output=True, encoding='utf-8')
        if r.returncode:
            self.fail(r.stderr)
        return r.stdout

    def package(self):
        return prepare(self.f.path, self.f.h, self.config, self.suppliers, self.receipt, self.root / 'packages', CONTRACT)

    def test_new_refusal_after_preview_prevents_upload_without_changing_payment(self):
        import admission
        package=self.package();before=self.f.view()
        admission.save(self.f.path,{'T':{'refusals':{'0001':{'reason':'本店拒收','active':True}}}},{'T':{}})
        with self.assertRaisesRegex(ValueError,'拒收|准入'):validate_for_upload(self.f.path,package,self.config,self.suppliers)
        self.assertEqual(self.f.view(),before)

    def reserve(self, p):
        return self.client.request('purchase.reserve', package_file=p['package_file'], package_sha256=p['package_sha256'])

    def test_native_package_durable_reserve_restart_no_business_writes(self):
        before, view = self.f.old_tables(), self.f.view()
        p = self.package()
        self.assertTrue(validate_for_upload(self.f.path, p, self.config, self.suppliers))
        first = self.reserve(p)
        self.assertTrue(first['created'])
        self.assertEqual(first['record']['state'], 'RESERVED')
        self.assertEqual(first['record']['totalQuantity'], '7')
        self.assertEqual(first['record']['totalAmount'], '0.98')
        self.assertEqual(p['intent']['paymentTotalAmount'], '0.99')
        self.assertEqual(p['intent']['roundingDelta'], '-0.01')
        self.assertFalse(self.reserve(p)['created'])
        self.assertEqual(self.client.request('purchase.list')['total'], 1)
        self.assertTrue(self.client.close())
        self.client = ProcessClient(COMPONENT, self.root / 'component')
        self.client.start()
        self.assertEqual(self.client.request('purchase.status', purchase_id=p['id'])['sequence'], first['record']['sequence'])
        self.assertFalse(self.reserve(p)['created'])
        self.assertEqual(self.f.old_tables(), before)
        self.assertEqual(self.f.view(), view)

    def test_wrong_excel_values_are_blocked_even_if_caller_rehashes(self):
        import openpyxl
        p = self.package()
        file = Path(p['file']['file'])
        wb = openpyxl.load_workbook(file)
        wb['sheet1']['D8'] = 8
        wb.save(file)
        wb.close()
        intent = p['intent']
        intent['file'].update(sha256=file_hash(file), bytes=file.stat().st_size)
        atomic_json(p['package_file'], intent)
        p['package_sha256'] = file_hash(p['package_file'])
        with self.assertRaises(ComponentError) as result:
            self.reserve(p)
        self.assertIn(result.exception.code, ['PURCHASE_INPUT_STRUCTURE', 'PURCHASE_INPUT_MISMATCH'])
        self.assertEqual(self.client.request('purchase.list')['total'], 0)

    def test_payment_change_stops_host_dispatch_and_reserved_order_remains_unique(self):
        p = self.package()
        self.reserve(p)
        self.f.save('payment', [{'付款单商品数量': 9, '实际金额': 1.99}])
        with self.assertRaisesRegex(ValueError, '付款单已更新'):
            validate_for_upload(self.f.path, p, self.config, self.suppliers)
        other = self.package()
        with self.assertRaises(ComponentError) as result:
            self.reserve(other)
        self.assertEqual(result.exception.code, 'PURCHASE_ALREADY_RESERVED')
        self.client.request('purchase.cancel', purchase_id=p['id'])
        self.assertTrue(self.reserve(other)['created'])

    def test_missing_browser_fails_precheck_then_retry_and_cancel_never_upload(self):
        p = self.package()
        self.reserve(p)
        for _ in range(2):
            self.client.request('purchase.start', purchase_id=p['id'])
            until = time.monotonic() + 5
            while time.monotonic() < until:
                state = self.client.request('purchase.status', purchase_id=p['id'])
                if not state['active']:
                    break
                time.sleep(.01)
            self.assertEqual(state['state'], 'FAILED_PRECHECK')
            self.assertFalse(state['uploadAttempted'])
            self.assertIsNone(state['taskId'])
        self.assertEqual(self.client.request('purchase.cancel', purchase_id=p['id'])['state'], 'CANCELLED_BEFORE_UPLOAD')
        self.assertIsNone(self.client.request('ping')['active_purchase_id'])

    def test_template_latest_pointer_change_blocks_before_reservation(self):
        p = self.package()
        atomic_json(self.receipt['latestFile'], {**self.receipt, 'attemptId': 'newer'})
        with self.assertRaises(ComponentError) as result:
            self.reserve(p)
        self.assertEqual(result.exception.code, 'PURCHASE_TEMPLATE_STALE')
        self.assertEqual(self.client.request('purchase.list')['total'], 0)

    def test_three_decimal_upload_price_and_legacy_unuploaded_preview_are_rejected(self):
        import zipfile
        for mode in ('three_decimal_price', 'legacy'):
            p = self.package()
            if mode == 'legacy':
                del p['intent']['pricePolicy']
            else:
                file = Path(p['file']['file'])
                with zipfile.ZipFile(file) as z:
                    entries = [(i, z.read(i.filename)) for i in z.infolist()]
                member = p['file']['modified_zip_member']
                with zipfile.ZipFile(file, 'w') as z:
                    for i, raw in entries:
                        if i.filename == member:
                            self.assertIn(b'<v>0.14</v>', raw)
                            raw = raw.replace(b'<v>0.14</v>', b'<v>0.141</v>')
                        z.writestr(i, raw)
                p['intent']['file'].update(sha256=file_hash(file), bytes=file.stat().st_size)
            atomic_json(p['package_file'], p['intent'])
            p['package_sha256'] = file_hash(p['package_file'])
            with self.subTest(mode=mode), self.assertRaises(ComponentError) as error:
                self.reserve(p)
            self.assertEqual(error.exception.code, 'PURCHASE_PRICE_POLICY' if mode == 'legacy' else 'PURCHASE_INPUT_MISMATCH')
            self.assertEqual(self.client.request('purchase.list')['total'], 0)

    def test_legacy_file_still_checks_exactly_for_binding_but_cannot_be_uploaded(self):
        from aoxiang_purchase import fingerprint
        from aoxiang_purchase_excel import generate
        p = self.package()
        plan = json.loads(Path(p['plan_file']).read_text('utf-8'))
        plan['price_basis'] = 'confirmed_payment_amount_column_F_price_left_blank'
        for r in plan['rows']:
            r['amount'] = r.pop('payment_amount')
            del r['unit_price'], r['rounding_delta']
        plan['total_amount'] = '0.99'
        del plan['payment_total_amount'], plan['rounding_delta']
        plan['fingerprint'] = fingerprint(plan)
        generated = generate(plan, self.receipt, CONTRACT, Path(p['plan_file']).parent/'legacy.xlsx', note=p['intent']['note'])
        intent = p['intent'];intent['basis']['planFingerprint'] = plan['fingerprint']
        for r in intent['rows']:
            r['amount'] = r.pop('paymentAmount')
            del r['unitPrice'], r['roundingDelta']
        intent['totalAmount'] = '0.99'
        for k in ('pricePolicy', 'paymentTotalAmount', 'roundingDelta'):del intent[k]
        intent['file'] = {'path':generated['file'],'sha256':generated['sha256'],'bytes':generated['bytes']}
        atomic_json(p['plan_file'],plan);p['plan_sha256'] = file_hash(p['plan_file'])
        atomic_json(p['package_file'],intent);p['package_sha256'] = file_hash(p['package_file'])
        self.assertTrue(validate_for_upload(self.f.path,p,self.config,self.suppliers,for_binding=True))
        with self.assertRaisesRegex(ValueError,'旧预览'):
            validate_for_upload(self.f.path,p,self.config,self.suppliers)
        self.node("import fs from 'node:fs';import {checkPurchaseInput} from './src/purchase-create-artifacts.mjs';const p=JSON.parse(fs.readFileSync(process.argv[1]));await checkPurchaseInput(p,{requireRounded:false});",p['package_file'])


if __name__ == '__main__':
    unittest.main()
