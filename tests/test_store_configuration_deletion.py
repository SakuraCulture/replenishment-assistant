"""Complete-row deletion, including 0.41.1 leftovers and legacy cached routes."""
import copy,json,os,sys,tempfile,unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import parameter_presets as pp,parameter_schema as schema,work_repository as wr
import replenishment_plans as plans,parameter_maintenance as pm
from store_parameter_import import base

class CompleteDeletion(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.repo=wr.Repository(self.tmp.name)
        a=base();a['ui'].update(primary='河北仓',secondary='山东仓',mode=2,amount='50');a['name']='测试A'
        self.repo.save_stores({'A':a,'B':a})
    def tearDown(self):self.tmp.cleanup()
    def test_route_only_leftover_is_removed_with_no_placeholder_after_reopen(self):
        pp.clear_defaults(self.repo,['A'],pp.snapshot(self.repo));b=self.repo.stores()['B']
        removed=pp.remove_store_configurations(self.repo,['A','A','MISSING'],pp.snapshot(self.repo))
        self.assertEqual(removed,['A']);self.assertEqual(wr.Repository(self.tmp.name).stores(),{'B':b})
        with closing(self.repo.connect()) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM stores WHERE code=?',('A',)).fetchone()[0],0)
        self.assertEqual(pp.remove_store_configurations(self.repo,['A'],pp.snapshot(self.repo)),[])
    def test_whole_batch_cas_and_failure_roll_back(self):
        before=self.repo.stores();expected=pp.snapshot(self.repo);b=copy.deepcopy(before['B']);b['ui']['amount']='60';self.repo.save_stores({'B':b})
        with self.assertRaises(wr.Conflict):pp.remove_store_configurations(self.repo,['A','B'],expected)
        current=self.repo.stores()
        with patch('settings_history.record',side_effect=RuntimeError('synthetic write failure')):
            with self.assertRaises(RuntimeError):pp.remove_store_configurations(self.repo,['A','B'],pp.snapshot(self.repo))
        self.assertEqual(self.repo.stores(),current)
    def test_presets_plans_and_global_mapping_unchanged(self):
        custom=pp.save(self.repo,'测试预案',schema.standard());pp.assign(self.repo,['A'],custom['id'],pp.snapshot(self.repo))
        plan_repo=plans.Repository(self.repo.path);plan_repo.save({'name':'测试线','parameter_mode':pp.MODE,'config':schema.standard(),
            'stores':[{'code':'A','name':'A','preset_id':None},{'code':'B','name':'B','preset_id':custom['id']}]})
        before=pp.snapshot(self.repo);mapping=pm.mapping_snapshot(self.repo.path)['mapping']
        pp.remove_store_configurations(self.repo,['A'],before);after=pp.snapshot(self.repo)
        self.assertEqual(after['presets'],before['presets']);self.assertEqual(after['plans'],before['plans'])
        self.assertEqual(after['stores']['B'],before['stores']['B']);self.assertEqual(pm.mapping_snapshot(self.repo.path)['mapping'],mapping)
    def test_existing_history_can_undo_explicitly(self):
        import settings_history as h
        before=self.repo.stores();pp.remove_store_configurations(self.repo,['A','B'],pp.snapshot(self.repo))
        self.assertEqual(self.repo.stores(),{});entry=h.history(self.repo)[0]
        h.apply(self.repo,h.plan(self.repo,entry));self.assertEqual(self.repo.stores(),before)

class UiCompleteDeletion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def test_saved_list_removal_current_scope_and_no_legacy_resurrection(self):
        from application_window import ApplicationWindow
        from test_parameter_reload import loaded,record
        from PySide6.QtCore import Qt
        import parameter_preset_ui as ui
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,REPLENISHMENT_DATA_DIR=tmp):
            w=ApplicationWindow()
            try:
                w.repository.save_budget('A',True,999);w.catalog_repository.save_route('A',{'primary':'河北仓','secondary':'山东仓'})
                w.work_repository.save_stores({'A':record(3),'B':record(4)})
                w.loaded_ok(loaded());scene=w.parameter_scene;scene.refresh();snap=w.snapshot();before=w.current_parameter_record('A')
                scene.table.item(scene.codes.index('A'),0).setCheckState(Qt.CheckState.Checked)
                with patch.object(ui,'review_rows',return_value=False):scene.delete_defaults()
                self.assertIn('A',scene.codes)
                with patch.object(ui,'review_rows',return_value=True):scene.delete_defaults()
                self.assertNotIn('A',scene.codes);self.assertNotIn('A',w.work_repository.stores());self.assertEqual(w.current_parameter_record('A'),before)
                scene.refresh();self.assertNotIn('A',scene.codes)
                scene.scope.setCurrentIndex(1);self.assertIn('A',scene.codes);self.assertIn('仅本次',scene.table.item(scene.codes.index('A'),4).text())
                scene.scope.setCurrentIndex(0);self.assertNotIn('A',scene.codes)
                w.reset_job();w.loaded_ok(loaded());self.assertEqual(w.store_ui('A')['mode'],0);self.assertEqual(w.store_ui('A')['amount'],'')
                self.assertIsNone(w.store_ui('A')['primary']);self.assertIsNone(w.store_ui('A')['secondary'])
                self.assertEqual(pp.signature(w.config_for('A')),pp.signature(schema.standard()));self.assertNotIn('A',w.work_repository.stores())
                w.restore_job({'day':wr.today(),'saved':'test','payload':snap});self.assertEqual(w.current_parameter_record('A'),before)
                w.work_repository.save_stores({'A':record(8)});w.reset_job();w.loaded_ok(loaded())
                self.assertEqual(w.config_for('A')['warehouses']['山东仓'],[8,3.5]);scene.refresh();self.assertIn('A',scene.codes)
            finally:
                import shiboken6
                w._discard_close=True;w.close();self.app.processEvents();shiboken6.delete(w)

if __name__=='__main__':unittest.main()
