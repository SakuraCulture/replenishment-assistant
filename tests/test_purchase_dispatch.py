"""Real host/Node boundary, with an offline request route and synthetic SQL/Excel."""
import copy
import json
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src')]
import test_aoxiang_purchase_tasks as fixtures
from aoxiang_purchase_tasks import validate_for_upload
from purchase_dispatch import facts


class PurchaseDispatchTests(unittest.TestCase):
    def setUp(self):
        self.task = fixtures.PurchaseTaskTests()
        self.task.setUp()
        self.package = self.task.package()
        self.f = self.task.f
        self.assertTrue(validate_for_upload(self.f.path, self.package, self.task.config, self.task.suppliers))
        self.assertTrue(self.task.client.close(timeout=10))

    def tearDown(self):
        self.task.tearDown()

    def node(self, mode='check'):
        file = self.f.root / 'dispatch-input.json'
        value = {'synthetic': True, 'fixture_root': str(self.f.root), 'business_path': str(self.f.path),
                 'component': str(self.task.root / 'component'), 'package': self.package,
                 'python': sys.executable, 'source': str(ROOT / 'src'), 'input_file': str(file),
                 'new_payment_plan': self.f.plan(self.f.scan('payment', [{'付款单商品数量': 2, '实际金额': .99}]))}
        file.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        r = subprocess.run([str(fixtures.NODE), str(ROOT / 'components/aoxiang/test/host-dispatch-fixture.mjs'), str(file), mode],
                           cwd=fixtures.COMPONENT, capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + '\n' + r.stderr)
        return json.loads(r.stdout.splitlines()[-1])

    def test_scoped_python_and_node_facts_are_identical(self):
        r = self.node()
        self.assertTrue(r['ok'], r)
        with closing(self.f.work.connect()) as db:
            self.assertEqual(r['facts'], facts(db, self.f.h))

    def test_current_payment_and_file_can_consume_exactly_once(self):
        self.assertTrue(self.node('consume')['ok'])
        r = self.node('consume')
        self.assertFalse(r['ok'])
        self.assertEqual(r['code'], 'HOST_SUBMISSION_REVIEW')
        with self.assertRaisesRegex(ValueError, '提交登记'):
            validate_for_upload(self.f.path, self.package, self.task.config, self.task.suppliers)

    def test_payment_changes_during_browser_prepare_do_not_upload(self):
        self.assert_pre_submit_rejected('prepare')

    def test_payment_changes_during_second_connection_do_not_upload(self):
        self.assert_pre_submit_rejected('connect')

    def test_payment_changes_at_actual_native_request_do_not_upload(self):
        self.assert_pre_submit_rejected('request')

    def assert_pre_submit_rejected(self, point):
        r = self.node(point)
        self.assertEqual(r['state'], 'FAILED_PRECHECK', r)
        self.assertEqual(r['error']['code'], 'PURCHASE_BASIS_CHANGED')
        self.assertFalse(r['uploadAttempted'])
        self.assertEqual(r['released'], 0)
        self.assertEqual(r['guard'], 'AUTHORIZED')
        self.assertEqual(self.f.view()['documents']['payment']['payload']['rows'][0]['base_quantity'], '2')

    def test_after_boundary_payment_save_is_allowed_without_resend(self):
        r = self.node('after_boundary')
        self.assertEqual((r['released'], r['state'], r['guard']), (1, 'ACCEPTED', 'CONSUMED'))
        self.assertEqual(r['quantity'], '7')
        self.assertEqual(self.f.view()['documents']['payment']['payload']['rows'][0]['base_quantity'], '2')

    def test_boundary_save_failure_is_proven_not_released_and_rearmable(self):
        for mode in ('mark_failure', 'mark_failure_after_write'):
            if mode.endswith('after_write'):
                self.task.tearDown(); self.setUp()
            with self.subTest(mode=mode):
                r = self.node(mode)
                self.assertEqual(r['released'], 0)
                self.assertFalse(r['uploadAttempted'], r)
                self.assertEqual(r['guard'], 'ABORTED')
                validate_for_upload(self.f.path, self.package, self.task.config, self.task.suppliers)
                self.assertTrue(self.node()['ok'])

    def test_unknown_release_keeps_original_boundary_and_cannot_replay(self):
        r = self.node('unknown_release')
        self.assertEqual((r['released'], r['state'], r['guard']), (1, 'NEEDS_REVIEW', 'CONSUMED'))
        self.assertTrue(r['uploadAttempted'])
        self.assertEqual(r['restart'], 'SUBMISSION_UNKNOWN')

    def test_cancel_browser_wait_does_not_enter_boundary(self):
        r = self.node('cancel')
        self.assertEqual((r['state'], r['released'], r['uploadAttempted']), ('PAUSED', 0, False))

    def test_same_catalog_items_with_new_metadata_still_pass(self):
        file = self.f.path.parent / 'warehouse_catalogs/河北仓.json'
        catalog = json.loads(file.read_text(encoding='utf-8'))
        catalog['imported_at'] = '2026-11-16T12:00:00'
        file.write_text(json.dumps(catalog, ensure_ascii=False, sort_keys=True, indent=2), encoding='utf-8')
        self.assertTrue(self.node()['ok'])

    def test_real_upstream_and_admission_changes_are_rejected(self):
        self.f.save('return', [{'门店确认数量': 4}])
        r = self.node()
        self.assertEqual(r['code'], 'PURCHASE_BASIS_CHANGED')

    def test_unrelated_store_admission_change_does_not_invalidate(self):
        import admission
        admission.save(self.f.path, {'OTHER': {'refusals': {}}}, {'OTHER': {}})
        self.assertTrue(self.node()['ok'])

    def test_other_warehouse_reissue_keeps_upload_permission_valid(self):
        import warehouse_execution as execution
        import normal_orders as normal
        import work_repository as wr
        with closing(self.f.work.connect()) as db, db:
            execution.ensure_parent(db, self.f.h['oid'])
        validate_for_upload(self.f.path, self.package, self.task.config, self.task.suppliers)
        payload = copy.deepcopy(self.f.payload)
        payload['result']['rows'][1]['D'] += ' 另一仓更正'
        digest = normal.fingerprint_owned(payload['result'], payload.get('new_focus'))
        with closing(self.f.work.connect()) as db, db:
            db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)', ('oid', 2, digest, wr.now(), normal.encode(payload, db)))
            db.execute('UPDATE normal_orders SET current=2 WHERE oid=?', ('oid',))
            execution.publish(db, 'oid', 2, digest, wr.now(), payload, ['山东仓'])
        self.assertTrue(self.node()['ok'])

    def test_changed_current_store_admission_and_corrupt_catalog_are_rejected(self):
        import admission
        admission.save(self.f.path, {'T': {'refusals': {'0001': {'reason': '合成拒收', 'active': True}}}}, {'T': {}})
        self.assertEqual(self.node()['code'], 'PURCHASE_BASIS_CHANGED')

    def test_catalog_items_changed_without_revision_is_not_accepted(self):
        file = self.f.path.parent / 'warehouse_catalogs/河北仓.json'
        catalog = json.loads(file.read_text(encoding='utf-8'))
        catalog['items']['0001']['可用数'] = 123456
        file.write_text(json.dumps(catalog, ensure_ascii=False), encoding='utf-8')
        self.assertEqual(self.node()['code'], 'PURCHASE_BASIS_CHANGED')


if __name__ == '__main__':
    unittest.main()
