"""Independent-warehouse receipt/export acceptance, all data is synthetic."""
import copy
import json
import sys
import unittest
from contextlib import closing
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src')]
import test_lifecycle as fixture
import normal_orders as normal
import purchases
import warehouse_execution as execution
import work_repository as wr
import lifecycle_comparison as comparison
from aoxiang_receipt_scope import assess
from aoxiang_workflows import Workflows


class WarehouseReceiptRevisionTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.LifecycleTests()
        self.f.setUp()

    def tearDown(self):
        self.f.tearDown()

    def paid(self, warehouse):
        h = next(h for h in purchases.targets(self.f.path) if h['warehouse'] == warehouse)
        self.f.h = h
        row = {'供货仓': warehouse, '商品编码': '0001' if warehouse == '河北仓' else '0002'}
        self.f.save('return', [row])
        self.f.save('payment', [row])
        with closing(self.f.work.connect()) as db, db:
            execution.ensure_parent(db, h['oid'])
        h = next(h for h in purchases.targets(self.f.path) if h['warehouse'] == warehouse)
        payment = self.f.view(h)['documents']['payment']
        basis = {k: h[k] for k in ('oid', 'revision', 'warehouse', 'store', 'number')}
        basis.update(signedDigest=h['digest'], visibilityToken=h.get('visibility_token', 0),
                     paymentVersion=payment['version'], paymentDigest=payment['digest'])
        return h, {'id': 'synthetic-po', 'basis': basis, 'binding': {'orderId': 'OFFLINE-PO', 'status': '待提交'}}

    def publish(self, warehouse):
        payload = copy.deepcopy(self.f.payload)
        for row in payload['result']['rows']:
            if row['AU'] == warehouse:
                row['D'] += ' 更正'
        digest = normal.fingerprint_owned(payload['result'], payload.get('new_focus'))
        with closing(self.f.work.connect()) as db, db:
            db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',
                       ('oid', 2, digest, wr.now(), normal.encode(payload, db)))
            db.execute('UPDATE normal_orders SET current=2 WHERE oid=?', ('oid',))
            execution.publish(db, 'oid', 2, digest, wr.now(), payload, [warehouse])

    def test_other_warehouse_revision_keeps_receipt_and_comparison_valid(self):
        for warehouse, other in [('河北仓', '山东仓'), ('山东仓', '河北仓')]:
            with self.subTest(warehouse=warehouse):
                if warehouse == '山东仓':
                    self.f.tearDown(); self.f.setUp()
                h, record = self.paid(warehouse)
                before = self.f.view(h)
                self.publish(other)
                self.assertTrue(assess(self.f.path, [record])[record['id']]['eligible'])
                self.assertEqual(self.f.view(h), before)
                self.assertEqual(len(comparison.collect(self.f.path, [h])), 1)
                current = [h for h in purchases.targets(self.f.path) if h['revision'] == h['current_revision']]
                self.assertEqual(len(comparison.collect(self.f.path, current)), 2)

    def test_own_warehouse_revision_still_rejects_old_purchase(self):
        _, record = self.paid('河北仓')
        self.publish('河北仓')
        self.assertFalse(assess(self.f.path, [record])[record['id']]['eligible'])

    def test_explicit_enable_recovers_wrong_retirement_without_auto_enable(self):
        h, record = self.paid('河北仓')
        store = Workflows(self.f.root / 'workflows', business_path=self.f.path)
        store.configure_schedules([record], 5, True)
        with store.db(True) as db:
            s = store.schedule(record['id'])
            s.update(retired=True, retired_reason='原签发修订已失效', enabled=False, automation_paused=True)
            db.execute('UPDATE ax_schedules SET payload=? WHERE id=?', (json.dumps(s), record['id']))
        self.publish('山东仓')
        store.retire_receipt_sources()
        self.assertFalse(store.schedule(record['id'])['enabled'])
        store.configure_schedules([record], 5, True)
        saved = store.schedule(record['id'])
        self.assertFalse(saved.get('retired', False))
        self.assertTrue(saved['enabled'])
        self.assertEqual(saved['order_id'], 'OFFLINE-PO')
        store.configure_schedules([record], 5, True)
        job = store.enqueue_due()
        self.assertEqual([i['order_id'] for i in job['items']], ['OFFLINE-PO'])
        self.assertIsNone(store.enqueue_due())


if __name__ == '__main__':
    unittest.main()
