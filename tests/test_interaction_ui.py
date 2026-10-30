"""Optional Qt contract tests. Pure regression suite remains runnable without Qt."""
import copy,os,sys,tempfile,unittest,importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
HAS_QT=importlib.util.find_spec('PySide6') is not None
if HAS_QT:
    from PySide6.QtWidgets import QApplication,QWidget,QPushButton,QCheckBox,QTableWidget,QComboBox
    from parameter_ui import ParameterDialog
    from batch_app import BatchWindow
    import work_repository as wr,parameter_schema as schema


@unittest.skipUnless(HAS_QT,'Qt interaction tests require the packaged runtime; pure tests remain portable')
class UiContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.parent=QWidget();self.parent.work_repository=wr.Repository(Path(self.tmp.name));self.config=schema.standard();self.preset=self.parent.work_repository.presets()[0];self.dialogs=[]
    def tearDown(self):
        import shiboken6
        for d in self.dialogs:shiboken6.delete(d)
        shiboken6.delete(self.parent);self.tmp.cleanup()
    def dialog(self,**kw):
        d=ParameterDialog(self.parent,self.config,self.preset,**kw);self.dialogs.append(d);return d
    def test_store_editor_cannot_persist_presets(self):
        d=self.dialog(scope='所选 1 家门店');texts=[b.text() for b in d.findChildren(QPushButton)]
        self.assertNotIn('保存方案新版本',texts);self.assertNotIn('复制另存方案',texts);self.assertTrue(d.retire_btn.isHidden())
        with patch.object(self.parent.work_repository,'save_preset') as save:d.save_preset(False);save.assert_not_called()
    def test_readonly_tabs_navigable_fields_locked(self):
        d=self.dialog(scope='生效参数',readonly=True);self.assertTrue(d.tabs.isEnabled());self.assertFalse(d.param_table.cellWidget(0,1).isEnabled());self.assertTrue(d.load_btn.isHidden())
        self.assertNotIn('保存方案新版本',[b.text() for b in d.findChildren(QPushButton)])
    def test_retire_confirmation_and_builtin_guard(self):
        custom=self.parent.work_repository.save_preset('custom',self.config);d=self.dialog(scope='方案库');idx=next(i for i in range(d.choices.count()) if d.choices.itemData(i)['id']==custom['id']);d.choices.setCurrentIndex(idx)
        with patch('interaction_ui.confirm',return_value=False):d.archive_selected()
        self.assertTrue(any(p['id']==custom['id'] for p in self.parent.work_repository.presets()))
        with patch('interaction_ui.confirm',return_value=True):d.archive_selected()
        self.assertFalse(any(p['id']==custom['id'] for p in self.parent.work_repository.presets()));self.assertFalse(d.retire_btn.isEnabled())
    def test_load_dirty_form_requires_confirmation(self):
        d=self.dialog(scope='所选 1 家门店');w=d.param_table.cellWidget(2,1);w.setValue(22)
        with patch('interaction_ui.confirm',return_value=False):d.load_selected()
        self.assertEqual(w.value(),22)
        with patch('interaction_ui.confirm',return_value=True):d.load_selected()
        self.assertEqual(d.param_table.cellWidget(2,1).value(),21)
    def test_apply_review_cancel_does_not_accept(self):
        d=self.dialog(scope='所选 2 家门店',store_configs={'B':self.config,'A':copy.deepcopy(self.config)});d.store_configs={'B':d.value(),'A':copy.deepcopy(d.value())};d.param_table.cellWidget(2,1).setValue(22)
        with patch('interaction_ui.review_rows',return_value=False) as review:d.finish('temporary');self.assertEqual(len(review.call_args.args[3]),2)
        self.assertIsNone(d.action);self.assertEqual(d.result(),0)
    def test_no_implicit_enter_on_mutations(self):
        d=self.dialog(scope='所选 1 家门店');buttons=d.findChildren(QPushButton)
        for b in buttons:
            if '应用' in b.text() and b.text()!='放弃未应用编辑':self.assertFalse(b.autoDefault());self.assertFalse(b.isDefault())
    def test_select_visible_vs_all(self):
        table=QTableWidget(2,1);a,b=QCheckBox(),QCheckBox();table.setRowHidden(1,True);host=SimpleNamespace(store_table=table,store_widgets={'a':{'row':0,'check':a},'b':{'row':1,'check':b}})
        BatchWindow.select_all(host,True,True);self.assertTrue(a.isChecked());self.assertFalse(b.isChecked());BatchWindow.select_all(host,True);self.assertTrue(b.isChecked());BatchWindow.select_all(host,False);self.assertFalse(a.isChecked());self.assertFalse(b.isChecked())
    def test_unedited_form_does_not_ask_to_discard(self):
        d=self.dialog(scope='方案库')
        with patch('interaction_ui.confirm') as ask:d.reject();ask.assert_not_called()
    def test_commit_cancel_waits_then_only_stops_remaining(self):
        import threading
        from PySide6.QtWidgets import QLabel
        from ui_work_io import Progress
        h=SimpleNamespace(committed=False,committing=False,cancelling=False,cancel_after_commit=False,operation='delivery_execute',cancel_btn=QPushButton(),note=QLabel(),_completed=False,exporting=False,session=SimpleNamespace(cancel=threading.Event()))
        h.reject=lambda:Progress.reject(h)
        Progress.commit_state(h,True);self.assertFalse(h.cancel_btn.isEnabled());h.reject();self.assertFalse(h.session.cancel.is_set())
        Progress.commit_state(h,False);self.assertTrue(h.session.cancel.is_set());self.assertTrue(h.committed);self.assertIn('修订',h.note.text());self.assertFalse(h.cancel_btn.isEnabled())
    def test_save_cannot_reenable_cancel_after_commit(self):
        from ui_work_io import Progress
        h=SimpleNamespace(committed=False,committing=True,cancelling=False,cancel_after_commit=False,operation='save',cancel_btn=QPushButton())
        Progress.commit_state(h,False);self.assertFalse(h.cancel_btn.isEnabled());self.assertIn('暂不能取消',h.cancel_btn.text())
