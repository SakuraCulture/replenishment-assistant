"""UI protocol checks; business expectations live in test_jst, not Qt callbacks."""
import os,sys,tempfile,unittest,importlib.util
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))

@unittest.skipUnless(importlib.util.find_spec('PySide6'),'optional Qt')
class JstUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,REPLENISHMENT_DATA_DIR=self.tmp.name);self.env.start();self.reader=patch('history_tasks.HistoryReader.request');self.reader_mock=self.reader.start()
        from application_window import ApplicationWindow
        self.win=ApplicationWindow()
    def tearDown(self):
        import shiboken6
        self.win._discard_close=True;self.win.close();self.app.processEvents();shiboken6.delete(self.win);self.reader.stop();self.env.stop();self.tmp.cleanup()
    def test_four_action_groups_fixed_names_and_daily_filters(self):
        w=self.win.followup
        self.assertEqual([w.actions.tabText(i) for i in range(4)],['流程处理','业务文件','依据复查','维护'])
        self.assertIn('待生成聚水潭订单',w.cards);self.assertIn('待核对付款单',w.cards)
        self.assertEqual(w.start.text(),w.end.text())
        w.query();request=self.reader_mock.call_args.args[0];self.assertEqual(request['kind'],'daily_batch')
    def test_generator_has_distinct_qt_finished_signal_and_worker_callback(self):
        from jst_ui import GenerationDialog
        with patch.object(GenerationDialog,'load_catalogs'):
            d=GenerationDialog(self.win,self.win,[]);self.app.processEvents()
            self.assertTrue(callable(d.worker_finished));self.assertTrue(hasattr(d.finished,'connect'))
            self.assertFalse(d.generate_btn.isEnabled());self.assertFalse(d._completed)
            d.reject();self.assertTrue(d._completed);d.deleteLater();self.app.processEvents()
    def test_commit_boundary_disables_stop(self):
        from jst_ui import GenerationDialog
        with patch.object(GenerationDialog,'load_catalogs'):
            d=GenerationDialog(self.win,self.win,[]);self.app.processEvents();d.boundary(True)
            self.assertFalse(d.stop_btn.isEnabled());self.assertTrue(d.committing)
            d.boundary(False);self.assertTrue(d.stop_btn.isEnabled());d.reject();d.deleteLater();self.app.processEvents()
    def test_generation_ready_and_old_callback_do_not_mark_paid(self):
        from jst_ui import GenerationDialog
        with patch.object(GenerationDialog,'load_catalogs'):
            d=GenerationDialog(self.win,self.win,[]);self.app.processEvents();d.auto_export.setChecked(False)
            d.saved_order({'gid':'G1','number':'N1','positive':True});d.saved_order({'gid':'G1','number':'N1','positive':True})
            self.assertEqual(d.completed.count(),1);self.assertEqual(d.saved,['G1']);self.assertEqual(d.export_queue,[])
            d.reject();d.deleteLater();self.app.processEvents()
    def test_close_requested_during_commit_stops_at_next_safe_boundary(self):
        import threading
        from types import SimpleNamespace
        from jst_ui import GenerationDialog
        with patch.object(GenerationDialog,'load_catalogs'):
            d=GenerationDialog(self.win,self.win,[]);self.app.processEvents()
            d.worker=SimpleNamespace(cancel=threading.Event());d.boundary(True);d.reject()
            self.assertTrue(d.close_pending);self.assertFalse(d.worker.cancel.is_set())
            d.boundary(False);self.assertTrue(d.worker.cancel.is_set())
            d.worker=None;d.reject();d.deleteLater();self.app.processEvents()

if __name__=='__main__':unittest.main()
