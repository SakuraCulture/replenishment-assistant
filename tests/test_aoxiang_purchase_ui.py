import copy
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))


class Client:
    def __init__(self, *args):
        self.running = False
        self.calls = []
        self.records = []

    def start(self):
        self.running = True

    def close(self, timeout=10):
        self.running = False
        return True

    def request(self, action, **params):
        self.calls.append((action, copy.deepcopy(params)))
        if action == 'configuration':
            return {'configured': True, 'enabled': True}
        if action == 'purchase.list':
            return {'total': len(self.records), 'items': copy.deepcopy(self.records)}
        if action == 'purchase.start':
            return {**self.records[0], 'active': True, 'state': 'VERIFYING', 'sequence': 3}
        if action == 'purchase.pause':
            return {**self.records[0], 'active': False, 'state': 'ACCEPTED', 'sequence': 4}
        if action == 'template.start':
            return {'id': 'tpl', 'state': 'RUNNING'}
        raise AssertionError('unexpected ' + action)


def record(**kw):
    return {'id': 'p1', 'state': 'ACCEPTED', 'sequence': 2, 'basis': {'number': 'BH-1', 'warehouse': '河北仓', 'store': 'T'},
            'target': {'buyerWarehouseCode': 'T', 'displayName': '测试店'}, 'supplierCode': '2078246', 'rows': 1,
            'totalQuantity': '7', 'totalAmount': '0.99', 'createdAt': '2026-11-02T00:00:00Z', 'updatedAt': '2026-11-02T00:00:00Z',
            'uploadAttempted': True, 'taskId': 'GEI-known', 'binding': None, 'error': None, 'additionalTaskIds': [], **kw}


class PurchaseUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from aoxiang_purchase_ui import PurchaseDialog
        self.temp = tempfile.TemporaryDirectory()
        self.errors = []
        self.warning = patch('readable_ui.failure', side_effect=lambda *args: self.errors.append(args[-1]))
        self.warning.start()
        host = SimpleNamespace(repository=SimpleNamespace(folder=Path(self.temp.name)), work_repository=SimpleNamespace(path=Path(self.temp.name) / 'unused.sqlite'))
        head = {'number': 'BH-1', 'name': '测试店', 'warehouse': '河北仓', 'store': 'T'}
        self.d = PurchaseDialog(None, host, head, client_factory=Client)
        self.d.show()
        self.wait(lambda: self.d.configuration is not None and self.d.worker is None)

    def wait(self, check):
        from PySide6.QtTest import QTest
        until = time.monotonic() + 5
        while not check() and time.monotonic() < until:
            self.app.processEvents()
            QTest.qWait(10)
        self.assertTrue(check())

    def tearDown(self):
        import shiboken6
        self.d.close()
        self.wait(lambda: self.d._can_close and self.d.worker is None)
        shiboken6.delete(self.d)
        self.warning.stop()
        self.temp.cleanup()

    def test_existing_task_only_verifies_original_and_pause_no_new_upload(self):
        d = self.d
        d.client.records = [record()]
        d.load()
        self.wait(lambda: d.worker is None)
        self.assertFalse(d.preview_btn.isEnabled())
        self.assertFalse(d.upload_btn.isEnabled())
        self.assertTrue(d.verify_btn.isEnabled())
        d.verify()
        self.wait(lambda: d.worker is None)
        self.assertTrue(d.stop_btn.isEnabled())
        d.stop()
        self.wait(lambda: d.worker is None)
        self.assertEqual([x for x in d.client.calls if x[0] == 'purchase.start'], [('purchase.start', {'purchase_id': 'p1'})])
        self.assertFalse(any(x[0] == 'purchase.reserve' for x in d.client.calls))

    def test_unknown_no_receipt_has_no_resend_button_and_late_old_state_cannot_replace(self):
        d = self.d
        d.client.records = [record(state='NEEDS_REVIEW', taskId=None)]
        d.load()
        self.wait(lambda: d.worker is None)
        self.assertFalse(d.upload_btn.isEnabled())
        self.assertFalse(d.verify_btn.isEnabled())
        self.assertFalse(d.cancel_btn.isEnabled())
        d.purchase_status(record(state='BOUND', sequence=4, binding={'orderId': 'PO-exact'}))
        d.purchase_status(record(sequence=3))
        self.assertEqual(d.current['state'], 'BOUND')
        self.assertIn('PO-exact', d.note.text())

    def test_template_error_does_not_generate_or_upload_and_close_is_cooperative(self):
        d = self.d
        self.assertTrue(d.preview_btn.isEnabled())
        d.fetch_template()
        self.wait(lambda: d.worker is None)
        d.template_status({'id': 'tpl', 'state': 'FAILED', 'stage': '模板下载失败', 'error': {'message': '未连接真实平台'}})
        self.assertIsNone(d.template_id)
        self.assertIsNone(d.package)
        self.assertFalse(d.upload_btn.isEnabled())
        self.assertEqual(self.errors, ['未连接真实平台'])
        self.assertFalse(any(x[0].startswith('purchase.start') for x in d.client.calls))

    def test_upload_confirmation_defaults_to_no_and_no_dispatch_on_cancel(self):
        from PySide6.QtWidgets import QMessageBox
        d = self.d
        d.package = {'intent': {'target': {'displayName': '测试店'}, 'supplierCode': '2078246', 'rows': [{}], 'totalQuantity': '7', 'totalAmount': '.99'}}
        with patch('aoxiang_purchase_ui.QMessageBox.question', return_value=QMessageBox.StandardButton.No) as question:
            d.upload()
        self.assertEqual(question.call_args.args[-1], QMessageBox.StandardButton.No)
        self.assertFalse(any(x[0] in ('purchase.reserve', 'purchase.start') for x in d.client.calls))


if __name__ == '__main__':
    unittest.main()
