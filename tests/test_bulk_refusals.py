import copy,json,sqlite3,sys,unittest
from contextlib import closing
from pathlib import Path
from threading import Event
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from test_admission_overview import Fixture
import admission,admission_overview as service
from work_repository import Conflict
from runtime_support import Cancelled


class BulkTests(Fixture,unittest.TestCase):
    def test_exact_store_sku_only_and_preserved_evidence(self):
        before=copy.deepcopy(self.values);data=self.load();p=service.prepare_release(data,[('A','001'),('A','001')])
        self.assertEqual(p['count'],1);self.assertEqual(data['records'],before);service.commit_release(self.path,p)
        after=admission.snapshot(self.path,'A')['record'];expected=copy.deepcopy(before['A']);expected['refusals']['001']['active']=False;self.assertEqual(after,expected)
        self.assertEqual(admission.snapshot(self.path,'ORPHAN')['record'],before['ORPHAN'])
    def test_conflict_rolls_back_entire_batch(self):
        other={'refusals':{'001':{'reason':'其他店','active':True}}};admission.save(self.path,{'B':other},{'B':{}})
        p=service.prepare_release(self.load(),[('A','001'),('B','001')]);changed=copy.deepcopy(other);changed['refusals']['001']['reason']='新原因';admission.save(self.path,{'B':changed},{'B':other})
        with self.assertRaises(Conflict):service.commit_release(self.path,p)
        self.assertTrue(admission.snapshot(self.path,'A')['record']['refusals']['001']['active']);self.assertEqual(admission.snapshot(self.path,'B')['record'],changed)
    def test_cancel_prepare_and_commit_does_not_write(self):
        e=Event();e.set();data=self.load();before=self.path.read_bytes()
        with self.assertRaises(Cancelled):service.prepare_release(data,[('A','001')],cancel=e)
        p=service.prepare_release(data,[('A','001')])
        with self.assertRaises(Cancelled):service.commit_release(self.path,p,cancel=e)
        self.assertEqual(self.path.read_bytes(),before)
    def test_missing_already_released_empty_are_rejected(self):
        for keys in ([],[('A','003')],[('A','missing')],[('missing','001')]):
            with self.assertRaises(ValueError):service.prepare_release(self.load(),keys)
    def test_expired_active_can_release_and_order_is_selection_order(self):
        p=service.prepare_release(self.load(),[('A','002'),('A','001')]);self.assertEqual([r[2] for r in p['rows']],['002','001']);service.commit_release(self.path,p)
        self.assertFalse(admission.snapshot(self.path,'A')['record']['refusals']['002']['active'])
    def test_commit_failure_rolls_back_all_rows_and_events(self):
        other={'refusals':{'001':{'reason':'其他店'}}};admission.save(self.path,{'B':other},{'B':{}})
        p=service.prepare_release(self.load(),[('A','001'),('B','001')])
        with closing(sqlite3.connect(self.path)) as db,db:
            count=db.execute('SELECT COUNT(*) FROM admission_events').fetchone()[0]
            db.execute("CREATE TRIGGER fail_b BEFORE UPDATE ON store_admission WHEN NEW.store='B' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END;")
        with self.assertRaises(sqlite3.IntegrityError):service.commit_release(self.path,p)
        self.assertTrue(admission.snapshot(self.path,'A')['record']['refusals']['001']['active'])
        with closing(sqlite3.connect(self.path)) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM admission_events').fetchone()[0],count)


class BulkUiTests(Fixture,unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def window(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from admission_overview_ui import Overview
        host=SimpleNamespace(work_repository=SimpleNamespace(path=self.path),store_widgets={},states={},worker=None,readonly=False,status=Mock(),invalidate=Mock())
        d=Overview(None,host,'refusals');d.data=self.load();d.filter();return d
    def test_checkbox_identity_and_all_filter(self):
        from PySide6.QtCore import Qt
        d=self.window();d.state.setCurrentText('全部');d.select_all(True);self.assertEqual(d.model.keys(),[('A','001'),('A','002')])
        d.model.setData(d.model.index(0,0),Qt.CheckState.Unchecked,Qt.ItemDataRole.CheckStateRole);self.assertEqual(d.model.keys(),[('A','002')]);d.close()
    def test_changed_filter_clears_hidden_selection(self):
        d=self.window();d.select_all(True);self.assertEqual(len(d.model.keys()),1);d.search.setText('临时');d.filter();self.assertFalse(d.model.keys());d.close()
    def test_same_filter_preserves_checks_for_action_and_clear_all(self):
        d=self.window();d.select_all(True);d.filter();self.assertEqual(len(d.model.keys()),1);d.select_all(False);self.assertFalse(d.model.keys());d.close()
    def test_cancel_preview_no_commit(self):
        d=self.window();d.select_all(True);before=self.path.read_bytes()
        with patch('admission_overview_ui.run',side_effect=lambda parent,fn,args,*a,**kw:fn(*args)),patch('interaction_ui.review_rows',return_value=False):d.release_checked()
        self.assertEqual(self.path.read_bytes(),before);self.assertEqual(len(d.model.keys()),1);d.close()
    def test_readonly_cannot_check_or_commit(self):
        d=self.window();d.host.readonly=True;d.update_controls();d.select_all(True);self.assertFalse(d.model.keys())
        with patch('admission_overview_ui.run') as run:d.release_checked();run.assert_not_called()
        d.close()

if __name__=='__main__':unittest.main()
