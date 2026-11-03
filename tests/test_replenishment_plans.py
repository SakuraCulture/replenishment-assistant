import copy,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import replenishment_plans as p,work_repository as wr,parameter_schema as schema

def value():return {'name':'合成线路','note':'合成测试','stores':[{'code':'B','name':'测试B'},{'code':'A','name':'测试A'},{'code':'C','name':'未到文件'}],'config':schema.standard()}

class Plans(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.work=wr.Repository(Path(self.tmp.name));self.repo=p.Repository(self.work.path)
    def tearDown(self):self.tmp.cleanup()
    def test_values_owned_ordered_and_no_route_budget(self):
        v=value();saved=self.repo.save(v);v['config']['warehouses']['山东仓']=[99,1]
        self.assertNotEqual(self.repo.list()[0]['config'],v['config']);self.assertEqual([i['code'] for i in saved['stores']],['B','A','C'])
        import restock_policy
        self.assertNotIn('budget',saved['config']);self.assertNotIn('route',saved['config']);self.assertIn(restock_policy.KEY,saved['config'])
    def test_conflict_and_delete_never_mutate_owned_snapshot(self):
        saved=self.repo.save(value());snapshot=copy.deepcopy(saved);new=self.repo.save({**saved,'name':'新名字'},saved)
        with self.assertRaises(wr.Conflict):self.repo.save(saved,saved)
        with self.assertRaises(wr.Conflict):self.repo.delete(saved)
        self.repo.delete(new);self.assertEqual(self.repo.list(),[]);self.assertEqual(snapshot,saved)
    def test_duplicate_and_invalid_parameters_reject(self):
        v=value();v['stores'].append(v['stores'][0])
        with self.assertRaises(ValueError):self.repo.save(v)
        v=value();v['config']['parameters']['probability']=2
        with self.assertRaises(ValueError):self.repo.save(v)

class WorkbenchPlans(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        from workbench import Workbench
        self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,REPLENISHMENT_DATA_DIR=self.tmp.name);self.env.start();self.w=Workbench()
        from test_parameter_reload import record
        self.w.work_repository.save_stores({'A':record(6),'B':record(4)})
        self.plan=p.Repository(self.w.work_repository.path).save(value())
    def tearDown(self):
        import shiboken6
        self.w._discard_close=True;self.w.close();self.app.processEvents();shiboken6.delete(self.w);self.env.stop();self.tmp.cleanup()
    def load(self):
        from test_parameter_reload import loaded
        self.w.loaded_ok(loaded())
    def test_before_import_plan_beats_store_default_and_preserves_budget_route(self):
        p.apply(self.w,self.plan);self.assertIsNone(self.w.loaded);self.load()
        self.assertEqual(self.w.selected_codes(),['B','A']);self.assertEqual(self.w.config_for('A'),self.plan['config']);self.assertEqual(self.w.store_ui('A')['mode'],1);self.assertEqual(self.w.store_ui('A')['primary'],'山东仓')
        self.assertIn('未到文件',self.w.plan_note.text());self.assertEqual(self.w.selected_settings('A')['preset']['plan_source']['id'],self.plan['id'])
    def test_recheck_preserves_per_store_adjustment_and_no_shared_objects(self):
        self.load();p.apply(self.w,self.plan);self.w.store_presets['A']['config']['parameters']['moq_days']=3
        self.load();self.assertEqual(self.w.config_for('A')['parameters']['moq_days'],3);self.assertNotEqual(self.w.config_for('B')['parameters']['moq_days'],3)
        self.assertEqual(self.w.active_plan,self.plan);self.assertEqual(self.w.selected_codes(),['B','A'])
    def test_snapshot_survives_plan_delete_and_restore(self):
        p.apply(self.w,self.plan);self.load();snap=self.w.snapshot();p.Repository(self.w.work_repository.path).delete(self.plan)
        self.w.reset_job();self.assertIsNone(self.w.active_plan)
        self.w.restore_job({'day':wr.today(),'saved':'test','payload':snap});self.assertEqual(self.w.active_plan,self.plan);self.assertEqual(self.w.config_for('A'),self.plan['config'])
    def test_different_current_configs_default_to_explicit_standard_source(self):
        from replenishment_plan_ui import Editor
        self.load();d=Editor(self.w)
        try:self.assertEqual(d.config,schema.standard());self.assertEqual(d.sources.currentIndex(),0);self.assertIn('系统标准',d.sources.currentText())
        finally:d.deleteLater()

if __name__=='__main__':unittest.main()
