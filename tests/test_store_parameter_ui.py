"""Qt preview and transaction boundaries, all settings in temporary databases."""
import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PySide6.QtWidgets import QApplication
import store_parameter_import as spi
import store_parameter_ui as ui
import work_repository as wr


class Host:
    def __init__(self, path):
        self.work_repository = wr.Repository(path)
        self.store_widgets = {'A': {}, 'B': {}}
        self.states = {c: {'name': c} for c in self.store_widgets}
        self.store_presets = {c: spi.base()['preset'] for c in self.store_widgets}
        self.overrides = {}
        self.uis = {c: spi.base()['ui'] for c in self.store_widgets}
        self.readonly = self.initializing = self._hydrating = False
        self.worker = None
        self.invalidated = []
    def preset_for(self, c):return copy.deepcopy(self.store_presets[c])
    def config_for(self, c):return self.preset_for(c)['config']
    def store_ui(self, c):return copy.deepcopy(self.uis[c])
    def set_store_ui(self, c, value):self.uis[c] = copy.deepcopy(value)
    def invalidate(self, codes):self.invalidated.extend(codes)
    def selection_changed(self):pass
    def refresh_parameter_label(self, code):pass


class TransactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app = QApplication.instance() or QApplication([])
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.host = Host(self.root)
        self.data = {'sources': {}, 'rows': [{'code': 'A', 'name': '', 'values': {('预算金额',): 0}, 'source': '测试/2'}]}
    def tearDown(self):self.tmp.cleanup()
    def commit(self, scope):
        snap = ui.capture(self.host, self.data)
        p = ui.preview(self.data, snap, scope)
        return ui.commit(self.host, self.data, snap, scope, p)
    def test_default_only_and_reload(self):
        before = copy.deepcopy(self.host.uis)
        self.commit('defaults')
        self.assertEqual(self.host.uis, before)
        self.assertEqual(self.host.invalidated, [])
        saved = wr.Repository(self.root).stores()
        self.assertEqual(saved['A']['ui']['amount'], '0')
    def test_current_only_invalidates_affected_store(self):
        before = copy.deepcopy(self.host.uis['B'])
        self.commit('current')
        self.assertEqual(self.host.invalidated, ['A'])
        self.assertEqual(self.host.uis['B'], before)
        self.assertEqual(self.host.work_repository.stores(), {})
    def test_both_saves_current_effective_config(self):
        self.host.store_presets['A']['config']['parameters']['cap_days'] = 18
        self.commit('both')
        self.assertEqual(self.host.work_repository.stores()['A']['preset']['config']['parameters']['cap_days'], 18)
        self.assertEqual(self.host.invalidated, ['A'])
    def test_defaults_conflict_and_database_failure_no_partial_current(self):
        for failure in (wr.Conflict('conflict'), OSError('disk full')):
            snapshot = ui.capture(self.host, self.data)
            p = ui.preview(self.data, snapshot, 'both')
            before = copy.deepcopy(self.host.uis)
            with patch.object(self.host.work_repository, 'save_stores', side_effect=failure):
                with self.assertRaises(type(failure)):ui.commit(self.host, self.data, snapshot, 'both', p)
            self.assertEqual(self.host.uis, before)
            self.assertEqual(self.host.invalidated, [])
    def test_real_stale_defaults_transaction_rejected(self):
        self.data['rows'].append({'code': 'B', 'name': '', 'values': {('预算金额',): 2}, 'source': '测试/3'})
        snapshot = ui.capture(self.host, self.data)
        p = ui.preview(self.data, snapshot, 'both')
        self.host.work_repository.save_stores({'B': spi.base()})
        with self.assertRaises(wr.Conflict):ui.commit(self.host, self.data, snapshot, 'both', p)
        self.assertNotIn('A', self.host.work_repository.stores())
        self.assertEqual(self.host.invalidated, [])
    def test_stale_current_readonly_and_worker_blocked(self):
        snapshot = ui.capture(self.host, self.data)
        p = ui.preview(self.data, snapshot, 'current')
        self.host.uis['A']['mode'] = 1
        with self.assertRaises(wr.Conflict):ui.commit(self.host, self.data, snapshot, 'current', p)
        self.host.readonly = True
        with self.assertRaises(ValueError):ui.commit(self.host, self.data, snapshot, 'current', p)
        self.host.readonly = False
        self.host.worker = object()
        with self.assertRaises(ValueError):ui.commit(self.host, self.data, snapshot, 'current', p)
    def test_preview_cancel_unknown_store_and_no_writes(self):
        self.data['rows'].append({'code': 'C', 'name': '新店', 'values': {}, 'source': '测试/3'})
        snap = ui.capture(self.host, self.data)
        d = ui.Preview(None, self.host, self.data, snap, 'current')
        self.assertFalse(d.go.isEnabled())
        d.scope.setCurrentIndex(0)
        self.assertTrue(d.go.isEnabled())
        self.assertTrue(d.findChildren(type(d.go))[-1].isDefault())
        d.reject()
        self.assertEqual(self.host.work_repository.stores(), {})
        d.deleteLater()
    def test_changed_file_rejected_after_preview(self):
        p = self.root / 'file.xlsx'
        p.write_bytes(b'before')
        data = {'sources': {str(p): spi.digest(p)}}
        p.write_bytes(b'after')
        with self.assertRaisesRegex(ValueError, '文件发生变化'):ui.verify_sources(data)


if __name__ == '__main__':unittest.main()
