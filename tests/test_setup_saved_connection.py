"""Opening settings is a local read, not permission to reconnect a browser."""
import copy
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtTest import QTest
from aoxiang_client import ProcessClient
from aoxiang_session import SessionGate, SessionOwner
from aoxiang_setup_ui import SetupDialog


CONFIG = {'configured': True, 'enabled': True, 'display_name': '隔离商家',
          'account_binding': {'merchant_code': 'QA'},
          'browser_binding': {'edge_executable': 'C:/saved/edge.exe', 'edge_version': 'test',
                              'actual_profile_path': 'C:/saved/Profile 1', 'profile_directory': 'Profile 1',
                              'output_root': 'C:/saved/downloads'},
          'targets': [{'buyerWarehouseCode': 'S', 'displayName': '隔离店', 'mappingVerifiedAt': '2026-11-08'}]}


class RetainedClient:
    def __init__(self):
        self.running = False; self.starts = self.closes = 0; self.calls = []
    def start(self):
        self.running = True; self.starts += 1
    def close(self, timeout=.5):
        self.running = False; self.closes += 1; return True
    def request(self, action, **kw):
        self.calls.append(action)
        if action == 'configuration': return copy.deepcopy(CONFIG)
        if action == 'session.health': return {'state': 'CONNECTED', 'login_state': 'VERIFIED'}
        if action == 'session.release': return {'idle': True}
        raise AssertionError('Opening/closing settings must not perform ' + action)


class SavedConnectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.host = QWidget(); self.client = RetainedClient()
        self.owner = SessionOwner(lambda: self.client); self.owner.start()
        self.host.aoxiang_workflows = NS(client_factory=ProcessClient, session=self.owner, gate=SessionGate())
        self.dialogs = []
        self.warning = patch('readable_ui.failure', side_effect=AssertionError); self.warning.start()
    def wait(self, predicate):
        end = time.monotonic() + 8
        while not predicate() and time.monotonic() < end:
            self.app.processEvents(); QTest.qWait(10)
        self.assertTrue(predicate())
    def new_dialog(self):
        d = SetupDialog(self.host, self.temp.name); self.dialogs.append(d); d.show()
        self.wait(lambda: d.worker is None)
        return d
    def close_dialog(self, d):
        d.close(); self.wait(lambda: d._can_close and d.worker is None)
    def tearDown(self):
        for d in self.dialogs: self.close_dialog(d)
        self.owner.close(); self.host.deleteLater(); self.app.processEvents()
        self.warning.stop(); self.temp.cleanup()
    def test_open_close_reopen_reuses_live_client_and_saved_binding_without_browser_actions(self):
        before = copy.deepcopy(CONFIG)
        for _ in range(2):
            d = self.new_dialog()
            self.assertEqual(d.edge.currentData(), 'C:/saved/edge.exe')
            self.assertEqual(d.profile.currentData(), 'C:/saved/Profile 1')
            self.assertEqual(d.output.text(), 'C:/saved/downloads')
            self.assertIn('登录已核验', d.status.text())
            d.data = {'items': [{'code': 'S', 'name': '隔离店'}, {'code': 'U', 'name': '未核验店'}]}; d.fill_directory()
            self.assertIn('已保存核验', d.table.item(0, 3).text())
            self.assertEqual(d.table.item(1, 3).text(), '尚未核验')
            self.assertFalse(d.save_btn.isEnabled())
            self.close_dialog(d)
            self.assertTrue(self.client.running)
        self.assertEqual((self.client.starts, self.client.closes), (1, 0))
        self.assertEqual(CONFIG, before)
        self.assertTrue(set(self.client.calls) <= {'configuration', 'session.health', 'session.release'})
    def test_explicit_discovery_prefers_saved_profile_and_never_substitutes_missing_profile(self):
        d = self.new_dialog()
        found = {'installations': [{'executable': 'C:/other/edge.exe', 'version': 'other'}, {'executable': 'C:/saved/edge.exe', 'version': 'saved'}],
                 'profiles': [{'actualProfilePath': 'C:/other/Default', 'displayName': '其他', 'profileDirectory': 'Default'},
                              {'actualProfilePath': 'C:/saved/Profile 1', 'displayName': '原配置', 'profileDirectory': 'Profile 1'}]}
        d.discovered((found, CONFIG))
        self.assertEqual(d.edge.currentIndex(), 1); self.assertEqual(d.profile.currentIndex(), 1)
        found['profiles'].pop(); d.discovered((found, CONFIG))
        self.assertEqual(d.profile.currentIndex(), -1)
        d.data = {'items': [{'code': 'S', 'name': '隔离店'}]}; d.fill_directory(); d.select_visible(True)
        self.assertFalse(d.verify_btn.isEnabled())
    def test_real_main_entry_does_not_retry_on_return_from_settings(self):
        from application_window import ApplicationWindow
        host = NS(browser_startup=NS(started=True, retry=Mock()))
        with patch('aoxiang_setup_ui.open_setup') as opened:
            ApplicationWindow.open_aoxiang_setup(host)
        opened.assert_called_once_with(host)
        host.browser_startup.retry.assert_not_called()
