"""Navigation contracts using isolated documents and unchanged business services."""
import copy
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import business_flow as flow


class ProjectionTests(unittest.TestCase):
    def test_order_and_overlap_without_mutation(self):
        rows = [dict(oid='b', next='payment', present={'return': True}, health={'return': False}),
                dict(oid='a', next='return'), dict(oid='done', next=None)]
        before = copy.deepcopy(rows)
        p = flow.home_projection(rows, [])
        self.assertEqual([r['oid'] for r in p['groups']['payment']], ['b'])
        self.assertEqual([r['oid'] for r in p['groups']['attention']], ['b'])
        self.assertEqual([r['oid'] for r in p['groups']['return']], ['a'])
        self.assertEqual(rows, before)

    def test_newest_unfinished_batch_only(self):
        batches = [dict(batch_id='closed', status='complete', counts={'done': 3}),
                   dict(batch_id='paused', status='paused', counts={'failed': 1}),
                   dict(batch_id='older', status='active', counts={'pending': 2})]
        self.assertEqual(flow.home_projection([], batches)['latest_batch']['batch_id'], 'paused')

    def test_budget_shortage_is_not_blocking(self):
        result = {'summary': dict(issues=0, unpriced=0, holding_gaps=0, candidate_amount=100, amount=0)}
        self.assertEqual(flow.review_state('已完成', result), 'ready')
        self.assertEqual(flow.review_state('待重算', result), 'blocking')
        result['summary']['unpriced'] = 1
        self.assertEqual(flow.review_state('已完成', result), 'review')

    def test_empty_scope_and_upstream_are_not_claimed_complete(self):
        self.assertIn('限定所选 0 单', flow.empty_message('payment', {'scope': []}, {'scope': 99}))
        self.assertIn('没有签发单', flow.empty_message('payment', {}, {'scope': 0}))
        self.assertIn('门店回传', flow.empty_message('payment', {'stage_filter': '待处理'}, {'scope': 1}))
        self.assertIn('仓或锁状态', flow.empty_message('payment', {'warehouse': '山东仓'}, {'scope': 1}))

    def test_real_lifecycle_projection_tracks_each_next_missing_document(self):
        from test_lifecycle import LifecycleTests
        fixture = LifecycleTests(); fixture.setUp()
        try:
            before = fixture.old_tables()
            for stage in ('return', 'payment', 'receipt'):
                p = flow.read_home(fixture.path, fixture.root / 'home-read.sqlite')
                matching = [r for r in p['groups'][stage] if r['warehouse'] == '河北仓']
                self.assertEqual(len(matching), 1)
                fixture.save(stage)
            p = flow.read_home(fixture.path, fixture.root / 'home-read.sqlite')
            self.assertFalse(any(r['warehouse'] == '河北仓' for r in p['groups']['receipt']))
            self.assertEqual(fixture.old_tables(), before)
        finally:
            fixture.tearDown()


@unittest.skipUnless(importlib.util.find_spec('PySide6'), 'Qt optional')
class InterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, REPLENISHMENT_DATA_DIR=self.tmp.name); self.env.start()
        self.reader = patch('history_tasks.HistoryReader.request'); self.request = self.reader.start()
        from application_window import ApplicationWindow
        self.win = ApplicationWindow()

    def tearDown(self):
        from shiboken6 import delete
        self.win._discard_close = True; self.win.job_dirty = False; self.win.close()
        self.app.processEvents(); delete(self.win); self.app.processEvents()
        self.reader.stop(); self.env.stop(); self.tmp.cleanup()

    def test_home_cancel_does_not_change_workbench(self):
        h = self.win.home_tasks
        before = copy.deepcopy(self.win.snapshot())
        h.refresh(); h.stop()
        self.assertFalse(h.busy)
        self.assertIsNone(h.data)
        self.assertEqual(self.win.snapshot(), before)
        h.refresh(); self.assertTrue(h.busy)

    def test_fixed_columns_share_model_and_click_original_index(self):
        from PySide6.QtWidgets import QTableWidgetItem
        from PySide6.QtTest import QSignalSpy
        t = self.win.overview; t.setRowCount(2)
        t.setItem(0, 0, QTableWidgetItem('B')); t.setItem(1, 0, QTableWidgetItem('A'))
        self.win.fixed_overview.sync()
        v = self.win.fixed_overview.view
        self.assertIs(v.model(), t.model())
        self.assertIs(v.selectionModel(), t.selectionModel())
        # Disconnect the data-dependent handler; verify only exact event routing.
        t.cellClicked.disconnect(self.win.open_overview)
        spy = QSignalSpy(t.cellClicked)
        v.clicked.emit(v.model().index(1, 0))
        self.assertEqual(spy.at(0), [1, 0])
        self.assertEqual([t.item(i, 0).text() for i in range(2)], ['B', 'A'])

    def test_nested_stage_keeps_fixed_scope_and_filter_reset_keeps_scope(self):
        from lifecycle_home_ui import LifecycleWidget
        heads = [dict(oid='fixed', warehouse='山东仓')]
        panel = LifecycleWidget(self.win, 'return', heads)
        panel.start.setText('2026-11-01'); panel.search.setText('测试')
        with patch('lifecycle_home_ui.StageDialog') as dialog:
            panel.open_stage('payment')
            self.assertEqual(dialog.call_args.args[2], heads)
        panel.clear_filters()
        self.assertEqual(panel.fixed_scope, heads)
        self.assertEqual(self.request.call_args.args[0]['scope'], [['fixed', '山东仓']])
        panel.shutdown(); panel.deleteLater()

    def test_legacy_mode_expands_existing_controls(self):
        self.assertFalse(self.win.catalog_maintenance_toggle.isChecked())
        self.win.master_legacy.setChecked(True)
        self.assertTrue(self.win.catalog_maintenance_toggle.isChecked())
        self.assertTrue(self.win.master_legacy.isChecked())

    def test_filter_does_not_reorder_or_change_export_inputs(self):
        from parameter_workspace_smoke import fixture
        import batch_service
        w = self.win; w.loaded_ok(fixture())
        for code in ('A', 'B'):
            w.store_widgets[code]['mode'].setCurrentIndex(1)
            r = batch_service.calculate_store(w.loaded, code, w.selected_settings(code), w.profile)
            w.results[code] = r; w.states[code]['state'] = '已完成'
        w.states['B']['state'] = '待重算'
        w.refresh_tables(); before = copy.deepcopy(w.results); order = list(w.overview_codes)
        w.review_filter.setCurrentIndex(1)
        self.assertEqual([s for i,s in enumerate(order) if not w.overview.isRowHidden(i)], ['B'])
        self.assertEqual(w.overview_codes, order)
        self.assertEqual(w.results, before)


if __name__ == '__main__':
    unittest.main()
