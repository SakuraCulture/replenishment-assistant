"""Isolated formal receipt tests, retaining original signed/payment facts."""
import copy
import json
import sys
import threading
import unittest
from contextlib import closing
from pathlib import Path

import openpyxl
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src')]
import test_aoxiang_purchase_tasks as fixture
import aoxiang_receipts as receipts
import order_lifecycle as lc
import purchases
import work_repository as wr
from aoxiang_directory import atomic_json, file_hash
from runtime_support import Cancelled


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.PurchaseTaskTests()
        self.f.setUp()
        self.package = self.f.package()
        self.p = self.package['intent']

    def tearDown(self):
        self.f.tearDown()

    def observation(self, quantity='5', amount='0.70'):
        scope = {'orderId': 'PO1', 'store': 'T', 'supplier': '2078246', 'status': '已完成',
                 # 0.99 / 7 -> 0.14, so the platform order is 0.98;
                 # the immutable confirmed payment remains 0.99.
                 'quantity': '7', 'amount': '0.98', 'receivedQuantity': quantity, 'receivedAmount': amount}
        main = {**scope, 'skuCount': '1', '_sourceRow': 5}
        row = {**scope, 'sku': self.p['rows'][0]['sku'], 'name': '合成商品', 'unit': '包', '_sourceRow': 5,
               'createdAt': '2026-11-01 12:00:00', 'updatedAt': '2026-11-02 12:00:00'}
        files = []
        for source, values in [('D16', main), ('D17', row)]:
            file = self.f.root / (source + '.xlsx')
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = '采购主单详情' if source == 'D16' else '采购子单详情'
            ws.append(list(values))
            for _ in range(3):
                ws.append([])
            ws.append(list(values.values()))
            wb.save(file)
            wb.close()
            files.append({'source': source, 'file': str(file), 'sha256': file_hash(file)})
        return {'schema': 1, 'operation': 'purchase.receipt.observation', 'purchaseId': self.p['id'],
                'runId': 'r1', 'identity': self.p['identity'], 'basis': self.p['basis'], 'orderId': 'PO1',
                'supplierCode': '2078246', 'observedAt': '2026-11-02T04:00:00Z', 'platformCompletedAt': None,
                'terminal': True, 'eligible': True, 'main': main, 'rows': [row], 'issues': [], 'files': files,
                'quantityBasis': 'native_actual_inventory_quantity_no_conversion'}

    def manifest(self, o):
        file = self.f.root / 'observation.json'
        atomic_json(file, o)
        return {'file': str(file), 'sha256': file_hash(file)}

    def prepare(self, manifest):
        return receipts.prepare(self.f.f.path, self.package, manifest, self.f.config, self.f.suppliers)

    def sync(self, manifest, **kw):
        return receipts.synchronize(self.f.f.path, self.package, manifest, self.f.config, self.f.suppliers, **kw)

    def test_short_received_and_repeat_after_lost_ack_no_new_version_or_original_changes(self):
        before = self.f.f.old_tables()
        old = self.f.f.view()['documents']
        m = self.manifest(self.observation())
        first = self.sync(m)
        self.assertEqual(first['version'], 1)
        second = self.sync(m)
        self.assertFalse(second['changed'])
        self.assertEqual(second['version'], 1)
        view = self.f.f.view()
        self.assertEqual(view['documents']['payment'], old['payment'])
        self.assertEqual(view['documents']['return'], old['return'])
        self.assertEqual(self.f.f.old_tables(), before)
        p = view['documents']['receipt']['payload']
        self.assertEqual(p['rows'][0]['base_quantity'], '5')
        self.assertEqual(p['rows'][0]['difference'], '-2')
        self.assertEqual(p['raw_rows'][0]['sources'][0]['row'], 5)
        self.assertIsNone(p['raw_rows'][0]['document_date'])
        self.assertEqual(len(p['files']), 3)
        for f in p['files']:
            with closing(purchases.connect(self.f.f.path)) as db:
                self.assertIsNotNone(db.execute('SELECT content FROM lifecycle_files WHERE sha=?', (f['sha'],)).fetchone())

    def test_real_zero_is_kept_null_blank_missing_and_mixed_status_never_become_zero(self):
        self.assertEqual(self.prepare(self.manifest(self.observation('0', '0')))['plans'][0]['payload']['rows'][0]['base_quantity'], '0')
        for kind in ('blank', 'null', 'missing', 'duplicate', 'mixed', 'unit', 'order', 'amount'):
            with self.subTest(kind=kind):
                o = self.observation()
                if kind == 'blank': o['rows'][0]['receivedQuantity'] = ''
                if kind == 'null': o['rows'][0]['receivedQuantity'] = None
                if kind == 'missing': o['rows'] = []
                if kind == 'duplicate': o['rows'] *= 2
                if kind == 'mixed': o['rows'][0]['status'] = '待提交'
                if kind == 'unit': o['rows'][0]['unit'] = ''
                if kind == 'order': o['rows'][0]['orderId'] = 'OTHER'
                if kind == 'amount': o['rows'][0]['amount'] = '1'
                with self.assertRaises(ValueError): self.prepare(self.manifest(o))
        self.assertFalse(self.f.f.view()['present']['receipt'])

    def test_payment_change_rejected_and_concurrent_update_at_commit_uses_original_lock_guard(self):
        m = self.manifest(self.observation())
        plan = self.prepare(m)
        self.f.f.save('payment', [{'付款单商品数量': 8, '实际金额': .99}])
        with self.assertRaises(wr.Conflict): lc.commit(self.f.f.path, plan)
        with self.assertRaisesRegex(ValueError, '付款单已更新'): self.prepare(m)
        self.assertFalse(self.f.f.view()['present']['receipt'])

    def test_manual_receipt_and_other_observation_not_overwritten(self):
        m = self.manifest(self.observation())
        plan = self.prepare(m)
        self.f.f.save('receipt', [{'实际收货数量': 3}])
        old = self.f.f.view()['documents']['receipt']
        with self.assertRaises(wr.Conflict): lc.commit(self.f.f.path, plan)
        with self.assertRaisesRegex(ValueError, '已有人工收货'): self.sync(m)
        self.assertEqual(self.f.f.view()['documents']['receipt'], old)

    def test_changed_files_and_cancel_do_not_write_formal_receipt(self):
        m = self.manifest(self.observation())
        plan = self.prepare(m)
        event = threading.Event(); event.set()
        with self.assertRaises(Cancelled): self.sync(m, cancel=event)
        with Path(plan['files'][1]['path']).open('ab') as stream: stream.write(b'changed')
        with self.assertRaisesRegex(ValueError, '原件已变化'): self.prepare(m)
        with self.assertRaisesRegex(ValueError, '原文件在预览后变化'): lc.commit(self.f.f.path, plan)
        self.assertFalse(self.f.f.view()['present']['receipt'])


if __name__ == '__main__':
    unittest.main()
