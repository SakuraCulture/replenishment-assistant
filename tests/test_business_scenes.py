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

if __name__=='__main__':unittest.main()
