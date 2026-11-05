import copy,json,os,sys,tempfile,unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import parameter_presets as pp,parameter_schema as schema,work_repository as wr
import store_defaults as sd,parameter_maintenance as pm,replenishment_plans as plans
from store_parameter_import import base

class Deletion(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.repo=wr.Repository(self.tmp.name)
        c=schema.standard();c['warehouses']['山东仓']=[3,2];self.p=pp.save(self.repo,'测试参数',c)
        a=base();a['ui'].update(primary='山东仓',secondary=None,mode=2,amount='123');a['name']='测试A'
        self.repo.save_stores({'A':a,'B':a});pp.assign(self.repo,['A','B'],self.p['id'],pp.snapshot(self.repo))
    def tearDown(self):self.tmp.cleanup()
    def raw(self,code):
        with closing(self.repo.connect()) as db:return json.loads(db.execute('SELECT data FROM stores WHERE code=?',(code,)).fetchone()[0])
    def test_delete_only_parameters_not_budget_route_or_other_store(self):
        before=self.repo.stores();pp.clear_defaults(self.repo,['A'],pp.snapshot(self.repo));after=self.repo.stores()
        self.assertNotIn('preset',self.raw('A'));self.assertEqual(after['A']['ui'],before['A']['ui'])
        self.assertEqual(after['A']['name'],before['A']['name']);self.assertEqual(after['B'],before['B'])
        self.assertFalse(sd.has_parameters(after['A']));self.assertEqual(pp.signature(after['A']['preset']['config']),pp.signature(schema.standard()))
        self.assertEqual(self.repo.list_jobs(),[])
    def test_atomic_conflict_and_repeat_no_recreation(self):
        expected=pp.snapshot(self.repo);changed=self.repo.stores()['B'];changed['ui']['amount']='456';self.repo.save_stores({'B':changed})
        with self.assertRaises(wr.Conflict):pp.clear_defaults(self.repo,['A'],expected)
        self.assertTrue(sd.has_parameters(self.repo.stores()['A']))
        pp.clear_defaults(self.repo,['A'],pp.snapshot(self.repo));first=self.raw('A')
        pp.clear_defaults(self.repo,['A'],pp.snapshot(self.repo));self.assertEqual(first,self.raw('A'))
    def test_follow_default_falls_back_but_explicit_plan_still_wins(self):
        plan=plans.Repository(self.repo.path).save({'name':'测试线路','parameter_mode':pp.MODE,'config':schema.standard(),
             'stores':[{'code':'A','name':'A','preset_id':None},{'code':'B','name':'B','preset_id':self.p['id']}]})
        frozen=pp.freeze(plan,self.repo);pp.clear_defaults(self.repo,['A','B'],pp.snapshot(self.repo));new=pp.freeze(plan,self.repo)
        self.assertEqual(frozen['_resolved_presets']['A']['config']['warehouses']['山东仓'],[3,2])
        self.assertEqual(pp.signature(new['_resolved_presets']['A']['config']),pp.signature(schema.standard()))
        self.assertEqual(new['_resolved_presets']['B']['config']['warehouses']['山东仓'],[3,2])
        self.assertEqual(new['_resolved_presets']['A']['parameter_source']['kind'],'自动填写')
    def test_legacy_global_mapping_preserved_when_last_default_deleted(self):
        for s,r in self.repo.stores().items():r['preset']['config']['category_profiles']={'测试分类':'快销'};self.repo.save_stores({s:r})
        before=pm.mapping_snapshot(self.repo.path)['mapping'];pp.clear_defaults(self.repo,['A','B'],pp.snapshot(self.repo))
        self.assertEqual(pm.mapping_snapshot(self.repo.path)['mapping'],before)
    def test_undo_and_restore_deleted_defaults(self):
        import settings_history as h
        before=self.repo.stores()['A'];pp.clear_defaults(self.repo,['A'],pp.snapshot(self.repo));entry=h.history(self.repo)[0]
        preview=h.plan(self.repo,entry);self.assertFalse(preview['conflicts']);h.apply(self.repo,preview)
        self.assertEqual(self.repo.stores()['A'],before)
        h.apply(self.repo,h.plan(self.repo,entry,restore=True));self.assertNotIn('preset',self.raw('A'))
    def test_route_only_update_keeps_absent_and_parameter_import_saves_again(self):
        from store_parameter_import import patch as patch_parameters
        pp.clear_defaults(self.repo,['A'],pp.snapshot(self.repo));r=self.repo.stores()['A']
        r=patch_parameters({'values':{('主仓',):'河北仓'}},r);self.repo.save_stores({'A':r});self.assertNotIn('preset',self.raw('A'))
        r=patch_parameters({'values':{('warehouses','山东仓',0):7}},self.repo.stores()['A']);self.repo.save_stores({'A':r})
        self.assertTrue(sd.has_parameters(self.repo.stores()['A']));self.assertEqual(self.repo.stores()['A']['preset']['config']['warehouses']['山东仓'][0],7)
    def test_delete_preset_can_release_references_without_selecting_builtin(self):
        plan=plans.Repository(self.repo.path).save({'name':'测试线','parameter_mode':pp.MODE,'config':schema.standard(),
            'stores':[{'code':'A','name':'A','preset_id':self.p['id']}]})
        pp.delete(self.repo,self.p['id'],pp.FOLLOW_DEFAULTS,pp.snapshot(self.repo))
        self.assertNotIn('preset',self.raw('A'));self.assertIsNone(plans.Repository(self.repo.path).list()[0]['stores'][0]['preset_id'])
        self.assertEqual(pp.visible(pp.snapshot(self.repo)),{})

class UiDeletion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        from application_window import ApplicationWindow
        self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,REPLENISHMENT_DATA_DIR=self.tmp.name);self.env.start();self.w=ApplicationWindow()
        from test_parameter_reload import record,loaded
        self.w.work_repository.save_stores({'A':record(3),'B':record(4)});self.w.loaded_ok(loaded());self.scene=self.w.parameter_scene;self.scene.refresh()
    def tearDown(self):
        import shiboken6
        self.w._discard_close=True;self.w.close();self.app.processEvents();shiboken6.delete(self.w);self.env.stop();self.tmp.cleanup()
    def test_cancel_then_confirm_current_and_historical_values_survive_new_job_fills(self):
        from test_parameter_reload import loaded
        from PySide6.QtCore import Qt
        import parameter_preset_ui as ui
        i=self.scene.codes.index('A');self.scene.table.item(i,0).setCheckState(Qt.CheckState.Checked)
        before=self.w.work_repository.stores();snap=self.w.snapshot();current=self.w.config_for('A')
        with patch.object(ui,'review_rows',return_value=False):self.scene.delete_defaults()
        self.assertEqual(self.w.work_repository.stores(),before)
        with patch.object(ui,'review_rows',return_value=True):self.scene.delete_defaults()
        self.assertEqual(self.w.config_for('A'),current);self.assertEqual(self.w.work_repository.stores()['B'],before['B'])
        self.assertEqual(self.scene.table.item(self.scene.codes.index('A'),4).text(),'未保存参数')
        self.w.reset_job();self.w.loaded_ok(loaded());self.assertEqual(pp.signature(self.w.config_for('A')),pp.signature(schema.standard()))
        self.w.restore_job({'day':wr.today(),'saved':'test','payload':snap});self.assertEqual(self.w.config_for('A'),current)
    def test_builtin_hidden_library_picker_and_first_store_no_default_write(self):
        from parameter_preset_ui import PresetLibrary,StorePlanEditor
        from parameter_ui import ParameterDialog
        d=PresetLibrary(self.w);e=StorePlanEditor(self.w);p=ParameterDialog(self.w,schema.standard(),self.w.preset_for('A'),scope='所选测试')
        try:
            self.assertEqual(d.values,[]);self.assertEqual(e.batch.findData('standard'),-1)
            self.assertTrue(all(not x.get('builtin') for x in p.presets))
            from test_parameter_reload import loaded
            self.w.work_repository.save_stores({'A':None});self.w.reset_job();self.w.loaded_ok(loaded())
            self.assertNotIn('A',self.w.work_repository.stores());self.assertEqual(pp.signature(self.w.config_for('A')),pp.signature(schema.standard()))
        finally:d.deleteLater();e.deleteLater();p.deleteLater()

if __name__=='__main__':unittest.main()
