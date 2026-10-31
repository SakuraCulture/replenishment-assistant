"""Navigation and default/current scope contracts, isolated from daily settings."""
import copy, os, sys, tempfile, unittest, importlib.util
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
HAS_QT=importlib.util.find_spec('PySide6') is not None

@unittest.skipUnless(HAS_QT,'optional Qt runtime')
class BusinessScenes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,REPLENISHMENT_DATA_DIR=self.tmp.name);self.env.start()
        self.reader=patch('history_tasks.HistoryReader.request');self.reader.start()
        from application_window import ApplicationWindow
        self.win=ApplicationWindow()
    def tearDown(self):
        import shiboken6
        self.win.close();self.app.processEvents();shiboken6.delete(self.win)
        self.reader.stop();self.env.stop();self.tmp.cleanup()
    def test_navigation_keeps_workbench_and_tracking_selection(self):
        w=self.win
        self.assertEqual(w.sections.count(),5)
        self.assertEqual([b.text() for b in w.top_nav],['首页','补货出单','决策复查','订单跟踪','参数设置'])
        self.assertIs(w.followup.parentWidget(),w.sections.widget(3))
        w.followup.records=[{'number':'sentinel'}];w.followup.checked={'sentinel':1}
        w.show_section(3);w.show_section(4);w.show_section(3)
        self.assertEqual(w.followup.checked,{'sentinel':1})
        self.app.processEvents();self.assertEqual(w.recent.rowCount(),0)
    def test_defaults_editor_has_no_current_apply(self):
        from parameter_ui import ParameterDialog
        from PySide6.QtWidgets import QPushButton
        p=self.win.preset_for('T');before=copy.deepcopy(self.win.store_presets)
        d=ParameterDialog(self.win,p['config'],p,scope='所选 1 店默认',intent='defaults',store_configs={'T':p['config']})
        texts=[b.text() for b in d.findChildren(QPushButton)]
        self.assertIn('保存为门店默认',texts);self.assertNotIn('应用到本次作业',texts)
        with patch('interaction_ui.review_rows',return_value=True) as review:d.finish('default')
        self.assertIn('当前作业和历史签发不变',review.call_args.args[4])
        self.assertEqual(self.win.store_presets,before)
        d.deleteLater();self.app.processEvents()
    def test_batch_workspace_keeps_scope_and_atomic_preview(self):
        import test_lifecycle as fixture
        from lifecycle_batch_ui import BatchImport
        import order_lifecycle as service
        f=fixture.LifecycleTests();f.setUp();old=self.win.work_repository;self.win.work_repository=f.work
        try:
            ds=[f.scan(s) for s in ('receipt','payment','return')]
            d=BatchImport(self.win,self.win,scope=[f.h])
            with patch('lifecycle_batch_ui.run',side_effect=lambda parent,fn,args,*a,**kw:fn(*args)):
                d.add_paths([s['files'][0]['path'] for s in ds]);self.assertFalse(d.issues);self.assertEqual(len(d.documents),3)
                d.preview();self.assertEqual([p['stage'] for p in d.plan['plans']],['return','payment','receipt']);self.assertFalse(d.review.save.isEnabled())
                d.review.complete.setChecked(True);d.review.extra_ack.setChecked(True);self.assertTrue(d.review.save.isEnabled())
                with patch('lifecycle_ui.commit',side_effect=lambda parent,host,plan:service.commit(host.work_repository.path,plan)):d.save()
                self.assertEqual(len(d.receipt),3);self.assertEqual(f.view()['status'],'已收齐')
            d.deleteLater();self.app.processEvents()
        finally:self.win.work_repository=old;f.tearDown()

if __name__=='__main__':unittest.main()
