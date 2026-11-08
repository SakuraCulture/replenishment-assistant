import copy
import json
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
from aoxiang_directory import atomic_json, file_hash


class Client:
    def __init__(self, *args):
        self.running = False
        self.calls = []
        self.receipt = {'id': 'p1', 'state': 'IDLE', 'sequence': 1, 'terminal': False, 'scope': {'orderId': 'PO1'}, 'active': False,
                        'summary': None, 'manifest': None, 'error': None,'basis':{'number':'BH-1','warehouse':'河北仓','store':'T'}}

    def start(self): self.running = True
    def close(self, timeout=10): self.running = False; return True
    def request(self, action, **kw):
        self.calls.append((action, copy.deepcopy(kw)))
        if action == 'configuration': return {'configured': True, 'enabled': True}
        if action == 'purchase.list': return {'total': 1, 'items': [{'id': 'p1', 'state': 'BOUND', 'basis': {'number': 'BH-1', 'warehouse': '河北仓', 'store': 'T'}, 'binding': {'orderId': 'PO1'}}]}
        if action == 'receipt.start': self.receipt.update(active=True, state='RUNNING', sequence=2)
        elif action == 'receipt.pause': self.receipt.update(active=False, state='PAUSED', sequence=3)
        elif action == 'receipt.synced': self.receipt.update(active=False, state='SYNCED', sequence=6)
        elif action != 'receipt.status': raise AssertionError(action)
        return copy.deepcopy(self.receipt)


class ReceiptUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def wait(self, predicate):
        from PySide6.QtTest import QTest
        until = time.monotonic() + 5
        while not predicate() and time.monotonic() < until:
            self.app.processEvents(); QTest.qWait(10)
        self.assertTrue(predicate())

    def setUp(self):
        # UI transport fixture has no business snapshots. Guard integration has
        # real isolated database coverage in test_aoxiang_receipt_scope.
        self.receipt_scope=patch('aoxiang_receipt_scope.require',return_value={});self.receipt_scope.start()
        from aoxiang_receipt_ui import ReceiptDialog
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.errors = []
        self.warning = patch('readable_ui.failure', side_effect=lambda *args: self.errors.append(args[-1]))
        self.warning.start()
        host = SimpleNamespace(repository=SimpleNamespace(folder=self.root), work_repository=SimpleNamespace(path=self.root / 'unused.db'))
        head = {'number': 'BH-1', 'name': '测试店', 'warehouse': '河北仓', 'store': 'T'}
        self.d = ReceiptDialog(None, host, head, client_factory=Client)
        self.d.show()
        self.wait(lambda: self.d.current is not None and self.d.worker is None)

    def tearDown(self):
        import shiboken6
        self.d.close()
        self.wait(lambda: self.d._can_close and self.d.worker is None)
        shiboken6.delete(self.d)
        self.warning.stop();self.receipt_scope.stop(); self.temp.cleanup()

    def proof(self, known=True):
        o = {'rows': [{'sku': '0001', 'name': '合成商品', 'unit': '包', 'quantity': '7', 'receivedQuantity': '0' if known else '', 'receivedAmount': '0' if known else ''}], 'issues': []}
        file = self.root / 'observation.json'
        atomic_json(file, o)
        manifest = {'file': str(file), 'sha256': file_hash(file)}
        self.d.client.receipt.update(state='READY', sequence=5, active=False, terminal=True, manifest=manifest, summary={'status': '已完成', 'observedAt': '2026-11-02T04:00:00Z'})
        return o

    def test_open_reads_locally_only_and_stop_preserves_original_task(self):
        d = self.d
        self.assertFalse(any(a in ('receipt.start', 'purchase.start', 'template.start') for a, _ in d.client.calls))
        d.start_sync(); self.wait(lambda: d.worker is None)
        self.assertTrue(d.stop_btn.isEnabled())
        d.stop(); self.wait(lambda: d.worker is None)
        self.assertEqual(d.current['state'], 'PAUSED')
        self.assertEqual([kw for a, kw in d.client.calls if a == 'receipt.start'], [{'purchase_id': 'p1'}])

    def test_ready_auto_save_after_sync_uses_existing_service_and_no_platform_resubmit(self):
        d = self.d
        context = d.folder / 'purchase-packages/p1/本地任务索引.json'
        context.parent.mkdir(parents=True)
        context.write_text('{}', encoding='utf-8')
        self.proof()
        with patch('aoxiang_receipt_ui.synchronize', return_value={'number': 'BH-1', 'revision': 1, 'stage': 'receipt', 'version': 1, 'changed': True}) as save:
            d.refresh_current()
            self.wait(lambda: d.current['state'] == 'SYNCED' and d.worker is None)
            self.assertEqual(save.call_count, 1)
        self.assertFalse(d.sync_btn.isEnabled())
        self.assertFalse(d.save_btn.isEnabled())
        self.assertIn('常规监控已停止', d.note.text())
        self.assertFalse(any(a == 'purchase.start' for a, _ in d.client.calls))

    def test_unknown_display_does_not_turn_into_zero_and_stale_response_is_ignored(self):
        d = self.d
        o = self.proof(False)
        d.client.receipt.update(state='NEEDS_REVIEW')
        d.render((copy.deepcopy(d.client.receipt), o))
        self.assertEqual(d.model.index(0, 4).data(), '未知')
        self.assertEqual(d.model.index(0, 6).data(), '未知')
        old = {**d.client.receipt, 'sequence': 2, 'state': 'RUNNING', 'active': True}
        d.render((old, None))
        self.assertEqual(d.current['state'], 'NEEDS_REVIEW')
        self.assertFalse(d.save_btn.isEnabled())

    def test_missing_host_package_keeps_final_pending_and_can_retry_without_new_download(self):
        d = self.d
        self.proof()
        d.refresh_current()
        self.wait(lambda: bool(self.errors) and d.worker is None)
        self.assertEqual(d.current['state'], 'READY')
        self.assertTrue(d.save_btn.isEnabled())
        self.assertIn('原采购付款依据未找到', self.errors[0])
        self.assertFalse(any(a == 'receipt.synced' for a, _ in d.client.calls))


if __name__ == '__main__': unittest.main()
