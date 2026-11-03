"""Same-window A -> persisted B -> failed-store retry uses B's complete snapshot."""
import copy,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from PySide6.QtWidgets import QApplication
from workbench import Workbench
from retry_profile_smoke import exercise,install,wait,quantities,profile

class RetryProfile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,REPLENISHMENT_DATA_DIR=self.tmp.name);self.env.start();self.win=Workbench()
    def tearDown(self):
        import shiboken6
        if self.win.worker:self.win.worker.cancel.set();wait(self.win)
        self.win._discard_close=True;self.win.close();self.app.processEvents();shiboken6.delete(self.win);self.env.stop();self.tmp.cleanup()
    def test_saved_B_retry_budget_10(self):
        r=exercise(self.win);self.assertEqual(r['task_a'],{'A':10.,'B':0.});self.assertEqual(r['actual'],{'A':0.,'B':10.});self.assertEqual(r['amount'],10.)
    def test_copy_restore_path(self):self.assertEqual(exercise(self.win,owned=False)['actual'],{'A':0.,'B':10.})
    def test_restored_no_profile_unlimited_does_not_reuse_A(self):self.assertIsNone(exercise(self.win,budget=None,restored_profile=None)['used_profile'])
    def test_restored_zero_budget(self):self.assertEqual(exercise(self.win,budget='0')['actual'],{'A':0.,'B':0.})
    def test_same_batch_retry_keeps_frozen_input_settings_and_profile(self):
        install(self.win,'A');self.win.start_calculation();wait(self.win);expected=copy.deepcopy(self.win.results['S1'])
        self.win.profile=profile('B');self.win.results.pop('S1');self.win.states['S1'].update(state='计算失败',error='合成瞬时失败')
        self.win.retry_stores(['S1']);wait(self.win);self.assertEqual(self.win.results['S1']['rows'],expected['rows'])
    def test_reset_removes_all_old_calculation_parts(self):
        install(self.win,'A');self.win.start_calculation();wait(self.win);self.win.reset_job()
        self.assertIsNone(self.win._calculation_input);self.assertEqual(self.win._calculation_settings,{})
        self.assertFalse(hasattr(self.win,'_calculation_profile'))
if __name__=='__main__':unittest.main()
