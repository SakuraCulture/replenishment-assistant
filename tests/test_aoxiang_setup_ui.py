import copy
import importlib.util
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))


class FakeClient:
    def __init__(self, *args):
        self.running = False
        self.calls = []
        self.job = None
        self.close_attempts = 0

    def start(self):
        self.running = True
        return {'configured': False}

    def request(self, action, **values):
        self.calls.append((action, copy.deepcopy(values)))
        if action == 'discover':
            return {'installations': [{'executable': 'C:/edge.exe', 'version': 'test'}],
                    'profiles': [{'displayName': '测试配置', 'profileDirectory': 'Default', 'actualProfilePath': 'C:/profile'}]}
        if action == 'configuration':
            return {'configured': False}
        if action == 'setup.start':
            self.job = {'id': 'setup1', 'state': 'RUNNING', 'stage': '核验', 'current': 0, 'total': len(values['targets']),
                        'identity': None, 'error': None, 'targets': [{**v, 'state': 'RUNNING', 'error': None} for v in values['targets']]}
        elif action == 'setup.status':
            self.job.update(state='READY', stage='待保存', current=self.job['total'], verified=self.job['total'], failed=0)
            for item in self.job['targets']:
                item['state'] = 'VERIFIED'
        elif action == 'setup.commit':
            self.job.update(state='SAVED', stage='已保存')
        elif action == 'setup.cancel':
            self.job.update(state='CANCELLED', stage='已停止')
        else:
            raise AssertionError(action)
        return copy.deepcopy(self.job)

    def close(self, timeout=10):
        self.close_attempts += 1
        self.running = False
        return True


@unittest.skipUnless(importlib.util.find_spec('PySide6'), 'optional Qt runtime')
class SetupUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from aoxiang_setup_ui import SetupDialog
        self.temp = tempfile.TemporaryDirectory()
        self.errors = []
        self.warning = patch('readable_ui.failure', side_effect=lambda *args: self.errors.append(args[-1]))
        self.warning.start()
        self.dialog = SetupDialog(None, self.temp.name, client_factory=FakeClient)
        self.dialog.show()
        self.wait(lambda: self.dialog.worker is None)
        self.dialog.data = {'items': [{'code': 'W2', 'name': '店2'}, {'code': 'W1', 'name': '店1'}]}
        self.dialog.fill_directory()

    def wait(self, check, timeout=6):
        from PySide6.QtTest import QTest
        until = time.monotonic() + timeout
        while not check() and time.monotonic() < until:
            self.app.processEvents()
            QTest.qWait(10)
        self.assertTrue(check(), 'UI wait timed out')

    def tearDown(self):
        import shiboken6
        self.dialog.close()
        self.wait(lambda: self.dialog._can_close and self.dialog.worker is None)
        shiboken6.delete(self.dialog)
        self.warning.stop()
        self.temp.cleanup()

    def test_selection_search_and_explicit_verify_then_save(self):
        d = self.dialog
        self.assertFalse(d.verify_btn.isEnabled())
        d.search.setText('店1')
        d.select_visible(True)
        self.assertEqual(d.selected(), [{'code': 'W1', 'name': '店1'}])
        d.detect()
        self.wait(lambda: d.worker is None)
        self.assertTrue(d.verify_btn.isEnabled())
        d.verify()
        self.wait(lambda: d.setup_state == 'READY' and d.worker is None)
        self.assertTrue(d.save_btn.isEnabled())
        self.assertFalse(d.import_btn.isEnabled())
        self.assertFalse(any(action == 'setup.commit' for action, _ in d.client.calls))
        d.save_setup()
        self.wait(lambda: d.setup_state == 'SAVED' and d.worker is None)
        self.assertTrue(d.import_btn.isEnabled())
        self.assertEqual(self.errors, [])

    def test_cancel_ready_preview_reenables_configuration(self):
        d = self.dialog
        d.select_visible(True)
        d.detect()
        self.wait(lambda: d.worker is None)
        d.verify()
        self.wait(lambda: d.setup_state == 'READY' and d.worker is None)
        d.stop_setup()
        self.wait(lambda: d.setup_state == 'CANCELLED' and d.worker is None)
        self.assertFalse(d.save_btn.isEnabled())
        self.assertTrue(d.verify_btn.isEnabled())
        self.assertFalse(any(action == 'setup.commit' for action, _ in d.client.calls))

    def test_discovery_is_not_full_verification_progress(self):
        d = self.dialog
        d.detect()
        self.wait(lambda: d.worker is None)
        self.assertEqual(d.progress.value(), 0)
        self.assertIn('核验尚未开始', d.progress.format())
        self.assertFalse(d.verify_btn.isEnabled())
        d.select_visible(True)
        self.assertTrue(d.verify_btn.isEnabled())

    def test_poll_failure_stops_repeated_dialogs_and_reconnect_recovers(self):
        from aoxiang_client import ComponentError
        from PySide6.QtTest import QTest
        d = self.dialog
        d.select_visible(True)
        d.detect()
        self.wait(lambda: d.worker is None)
        original = d.client.request
        def request(action, **values):
            if action == 'setup.status':
                raise ComponentError('RESPONSE_UNKNOWN', '模拟状态请求超时')
            return original(action, **values)
        d.client.request = request
        d.verify()
        self.wait(lambda: d.transport_failed and d.worker is None)
        self.assertFalse(d.poll.isActive())
        self.assertFalse(d.clock.isActive())
        self.assertTrue(d.reconnect_btn.isEnabled())
        self.assertFalse(d.save_btn.isEnabled())
        self.assertEqual(len(self.errors), 1)
        QTest.qWait(700)
        self.assertEqual(len(self.errors), 1)
        old = d.client
        d.reconnect()
        self.wait(lambda: d.client is not old and not d.worker and not d.detect_pending)
        self.assertEqual(old.close_attempts, 1)
        self.assertIsNone(d.setup_id)
        self.assertTrue(d.verify_btn.isEnabled())
        d.verify()
        self.wait(lambda: d.setup_state == 'READY' and d.worker is None)

    def test_setup_failure_enables_retry_and_shows_code(self):
        d = self.dialog
        d.setup_updated({'id': 'failed', 'state': 'FAILED', 'stage': '核验门店', 'current': 0, 'total': 2,
                         'targets': [], 'error': {'code': 'SETUP_STEP_TIMEOUT', 'message': '步骤超时'}})
        self.assertFalse(d.poll.isActive())
        self.assertIn('SETUP_STEP_TIMEOUT', d.status.text())
        self.assertFalse(d.save_btn.isEnabled())

    def test_close_waits_for_live_worker_keeps_event_loop_responsive(self):
        d = self.dialog
        release, started = threading.Event(), threading.Event()
        def operation(progress):
            started.set()
            release.wait(5)
        d.run(operation, lambda _: None)
        self.wait(started.is_set)
        d.close()
        self.assertTrue(d._closing)
        self.assertFalse(d._can_close)
        self.assertEqual(d.client.close_attempts, 0)
        self.app.processEvents()
        self.assertTrue(d.worker.isRunning())
        release.set()
        self.wait(lambda: d._can_close and d.worker is None)
        self.assertEqual(d.client.close_attempts, 1)

    def test_close_pending_component_is_not_reported_closed(self):
        d = self.dialog
        original = d.client.close
        calls = []
        def closing(timeout=10):
            calls.append(timeout)
            return False if len(calls) == 1 else original(timeout)
        d.client.close = closing
        d.reject()
        self.wait(lambda: len(calls) >= 1 and d.worker is None)
        self.assertFalse(d._can_close)
        self.assertFalse(d.close_btn.isEnabled())
        self.wait(lambda: d._can_close and d.worker is None)
        self.assertEqual(len(calls), 2)

    def test_main_entry_does_not_clear_results_or_manual_changes(self):
        import shiboken6
        from application_window import ApplicationWindow
        from PySide6.QtCore import QTimer
        with patch.dict(os.environ, REPLENISHMENT_DATA_DIR=str(Path(self.temp.name) / 'main')):
            with patch('history_tasks.HistoryReader.request'):
                host = ApplicationWindow()
        host.results = {'S': {'sentinel': [1, 2, 3]}}
        host.preissue_drafts = {'S': {'edits': {'SKU': {'quantity': 24}}}}
        before = copy.deepcopy((host.results, host.preissue_drafts, host.sales.paths))
        def close_dialog():
            dialog = getattr(host, '_aoxiang_dialog', None)
            if dialog is None or dialog.worker:
                QTimer.singleShot(10, close_dialog)
            else:
                dialog.close()
        QTimer.singleShot(10, close_dialog)
        try:
            host.open_aoxiang_setup()
            self.assertEqual((host.results, host.preissue_drafts, host.sales.paths), before)
            self.assertIsNone(host._aoxiang_dialog)
        finally:
            host.results = {}
            host.preissue_drafts = {}
            host.close()
            self.app.processEvents()
            shiboken6.delete(host)


if __name__ == '__main__':
    unittest.main()
