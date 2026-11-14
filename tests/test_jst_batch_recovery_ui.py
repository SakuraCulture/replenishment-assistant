"""Real Qt recovery controls; isolated synthetic allocation database."""
import copy,os,sys,sqlite3,unittest
from pathlib import Path
from types import SimpleNamespace
from contextlib import closing
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import test_jst as fixture
import jst_batches as batches

class RecoveryUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        from jst_ui import GenerationDialog
        self.f=fixture.Service();self.f.setUp();self.other=self.f.second_store();self.path=self.f.path
        self.host=SimpleNamespace(work_repository=SimpleNamespace(path=self.path),repository=SimpleNamespace(folder=Path(self.path).parent),closing=False)
        plan=batches.preview_new(self.path,[self.f.h,self.other],{'河北仓':fixture.catalog(stock=100)})
        self.prepared=batches.register(self.path,plan);self.bid=plan['payload']['batch_id']
        with patch.object(GenerationDialog,'load_catalogs'):
            self.dialog=GenerationDialog(None,self.host,[self.f.h]);self.app.processEvents()
        self.dialog.catalogs={'河北仓':{'catalog':fixture.catalog(stock=700)}}
        self.warning=patch('jst_ui.warn');self.warn=self.warning.start()
    def tearDown(self):
        import shiboken6
        self.dialog.reject();shiboken6.delete(self.dialog);self.app.processEvents();self.warning.stop();self.f.tearDown()
    def context(self):
        with closing(sqlite3.connect(self.path)) as db:return batches.context(db,self.bid)
    def drive(self,action):
        from PySide6.QtCore import QTimer
        from jst_batch_recovery_ui import RecoveryDialog
        original=RecoveryDialog.exec
        def execute(d):
            self.assertEqual(len(d.groups[0]['selected']),1)
            self.assertIn('剩余2单',d.resume.text())
            QTimer.singleShot(0,{'cancel':d.reject,'restart':d.restart.click,'resume':d.resume.click}[action])
            return original(d)
        with patch.object(RecoveryDialog,'exec',execute):self.dialog.preview_btn.click()
    def test_return_keeps_all_claims_and_does_not_generate(self):
        before=self.context();self.drive('cancel')
        self.assertEqual(self.context(),before);self.assertIsNone(self.dialog.plan)
        self.assertFalse(self.dialog.generate_btn.isEnabled());self.assertIn('未改动',self.dialog.status.text());self.warn.assert_not_called()
    def test_restart_selected_previews_new_stock_without_generating(self):
        self.drive('restart');self.warn.assert_not_called()
        self.assertIsNone(self.dialog.batch_id);self.assertTrue(self.dialog.generate_btn.isEnabled())
        self.assertEqual([m['status'] for m in self.context()['members']],['cancelled','pending'])
        self.assertEqual(self.dialog.plan['payload']['plans'][0]['rows'][0]['stock'],'700')
        with closing(sqlite3.connect(self.path)) as db:self.assertEqual(db.execute('SELECT count(*) FROM jst_generations').fetchone()[0],0)
    def test_resume_previews_original_all_remaining_stock_and_order(self):
        before=self.context();self.drive('resume');self.warn.assert_not_called()
        self.assertEqual(self.context(),before);self.assertEqual(self.dialog.batch_id,self.bid)
        self.assertFalse(self.dialog.update_btn.isEnabled());self.assertEqual(self.dialog.order_list.count(),2)
        self.assertEqual([p['rows'][0]['quantity'] for p in self.dialog.plan['payload']['plans']],['40','60'])
        self.assertTrue(all(p['rows'][0]['stock']=='100' for p in self.dialog.plan['payload']['plans']))
    def test_changed_claim_reports_failure_without_releasing_other_item(self):
        from PySide6.QtCore import QTimer
        from jst_batch_recovery_ui import RecoveryDialog
        original=RecoveryDialog.exec
        def execute(d):
            def late():batches.pause(self.path,self.bid);d.restart.click()
            QTimer.singleShot(0,late);return original(d)
        with patch.object(RecoveryDialog,'exec',execute):self.dialog.preview_btn.click()
        self.assertEqual([m['status'] for m in self.context()['members']],['pending','pending'])
        self.assertIsNone(self.dialog.plan);self.assertFalse(self.dialog.generate_btn.isEnabled());self.warn.assert_called_once()
        self.assertIn('原批任务已变化',str(self.warn.call_args.args[1]))

if __name__=='__main__':unittest.main()
