import copy,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import parameter_presets as pp,parameter_schema as schema,replenishment_plans as plans
import work_repository as wr,parameter_maintenance as pm
from store_parameter_import import base
from workbench_actions import plan_record


def standard():
    # Persisted JSON uses lists for warehouse cycles, as in all prior versions.
    return wr.loads(wr.dumps(schema.standard()))


class Presets(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.repo=wr.Repository(self.tmp.name)
        self.a=base();self.a['ui'].update(primary='山东仓',mode=2,amount='100')
        self.b=base();self.b['ui'].update(primary='河北仓',secondary='山东仓',mode=1)
        self.repo.save_stores({'A':self.a,'B':self.b});self.a,self.b=[self.repo.stores()[s] for s in ('A','B')]
        c=standard();c['warehouses']['山东仓']=[8,2];self.p=pp.save(self.repo,'活动参数',c)
    def tearDown(self):self.tmp.cleanup()
    def plan(self):
        return plans.Repository(self.repo.path).save({'name':'线路','config':standard(),'parameter_mode':pp.MODE,
            'stores':[{'code':'B','name':'乙店','preset_id':None},{'code':'A','name':'甲店','preset_id':self.p['id']}]})
    def test_defaults_assign_only_selected_and_keep_budget_route(self):
        old=self.repo.stores();pp.assign(self.repo,['A'],self.p['id'],pp.snapshot(self.repo));new=self.repo.stores()
        self.assertEqual(old['B'],new['B']);self.assertEqual(old['A']['ui'],new['A']['ui'])
        self.assertEqual(new['A']['preset']['config'],self.p['config'])
    def test_system_default_cannot_edit_delete_archive(self):
        with self.assertRaises(ValueError):pp.save(self.repo,'改掉',standard(),'standard')
        with self.assertRaises(ValueError):pp.delete(self.repo,'standard',self.p['id'],pp.snapshot(self.repo))
        with self.assertRaises(ValueError):self.repo.archive_preset('standard',1)
    def test_update_future_defaults_not_existing_frozen_job(self):
        pp.assign(self.repo,['B'],self.p['id'],pp.snapshot(self.repo));p=self.plan();f=pp.freeze(p,self.repo)
        c=copy.deepcopy(self.p['config']);c['warehouses']['山东仓']=[4,3]
        pp.save(self.repo,'活动参数修改',c,self.p['id'],pp.snapshot(self.repo))
        self.assertEqual(f['_resolved_presets']['B']['config'],self.p['config'])
        self.assertEqual(pp.freeze(p,self.repo)['_resolved_presets']['B']['config'],c)
        self.assertEqual(pp.freeze(f,self.repo),f)
    def test_edited_default_detaches_and_no_silent_overwrite(self):
        pp.assign(self.repo,['B'],self.p['id'],pp.snapshot(self.repo));r=self.repo.stores()['B'];r['preset']['config']['parameters']['moq_days']=3
        self.repo.save_stores({'B':r});self.assertNotIn('default_binding',self.repo.stores()['B']['preset'])
        pp.save(self.repo,'更新',standard(),self.p['id']);self.assertEqual(self.repo.stores()['B']['preset']['config']['parameters']['moq_days'],3)
    def test_delete_references_atomically_and_preserve_snapshot(self):
        pp.assign(self.repo,['B'],self.p['id'],pp.snapshot(self.repo));p=self.plan();f=pp.freeze(p,self.repo)
        with self.assertRaises(ValueError):pp.delete(self.repo,self.p['id'],None,pp.snapshot(self.repo))
        pp.delete(self.repo,self.p['id'],'standard',pp.snapshot(self.repo))
        self.assertNotIn(self.p['id'],pp.latest(pp.snapshot(self.repo)))
        self.assertEqual(self.repo.stores()['B']['preset']['id'],'standard')
        new=plans.Repository(self.repo.path).list()[0];self.assertEqual(new['stores'][1]['preset_id'],'standard');self.assertEqual(new['revision'],p['revision']+1)
        self.assertEqual(f['_resolved_presets']['A']['id'],self.p['id'])
        self.assertEqual(self.repo.stores()['A'],self.a)
    def test_concurrent_change_rejects_delete_and_assignment(self):
        expected=pp.snapshot(self.repo);self.plan()
        with self.assertRaises(wr.Conflict):pp.delete(self.repo,self.p['id'],'standard',expected)
        with self.assertRaises(wr.Conflict):pp.assign(self.repo,['A'],self.p['id'],expected)
        self.assertEqual(self.repo.stores()['A'],self.a)
    def test_per_store_full_replacement_and_global_mapping(self):
        f=pp.freeze(self.plan(),self.repo);mapping=pm.mapping_snapshot(self.repo.path)
        for s,r in [('B',self.b),('A',self.a)]:
            target=plan_record(f,r,mapping,s)
            self.assertEqual(target['ui'],r['ui']);self.assertEqual(target['preset']['config'],self.p['config'] if s=='A' else standard())
        self.assertEqual([i['code'] for i in f['stores']],['B','A'])
        with self.assertRaises(ValueError):plan_record(self.plan(),self.a,mapping,'A')
    def test_persisted_plan_never_reuses_resolution_and_missing_ref_rejected(self):
        p=self.plan();f=pp.freeze(p,self.repo);new=plans.Repository(self.repo.path).save(f,p)
        self.assertNotIn('_resolved_presets',new)
        new['stores'][0]['preset_id']='missing'
        with self.assertRaises(ValueError):plans.Repository(self.repo.path).save(new,plans.Repository(self.repo.path).list()[0])
    def test_restoring_defaults_does_not_revive_deleted_reference(self):
        pp.assign(self.repo,['A'],self.p['id'],pp.snapshot(self.repo));old=self.repo.stores()['A']
        pp.delete(self.repo,self.p['id'],'standard',pp.snapshot(self.repo));self.repo.save_stores({'A':old})
        restored=self.repo.stores()['A'];self.assertEqual(restored['preset']['config'],old['preset']['config'])
        self.assertNotIn('default_binding',restored['preset'])


class WorkbenchPresets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        from workbench import Workbench
        self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,REPLENISHMENT_DATA_DIR=self.tmp.name);self.env.start();self.w=Workbench()
        from test_parameter_reload import record,loaded
        self.w.work_repository.save_stores({'A':record(6),'B':record(4)});self.w.loaded_ok(loaded())
        self.p=pp.save(self.w.work_repository,'活动',standard());self.plan=plans.Repository(self.w.work_repository.path).save({'name':'逐店','config':standard(),'parameter_mode':pp.MODE,
          'stores':[{'code':'B','name':'乙店','preset_id':self.p['id']},{'code':'A','name':'甲店','preset_id':None}]})
    def tearDown(self):
        import shiboken6
        self.w._discard_close=True;self.w.close();self.app.processEvents();shiboken6.delete(self.w);self.env.stop();self.tmp.cleanup()
    def test_apply_recheck_manual_and_restore_keep_frozen_parameters(self):
        from test_parameter_reload import loaded
        plans.apply(self.w,self.plan);self.assertEqual(self.w.config_for('A')['warehouses']['山东仓'][0],6)
        self.assertEqual(self.w.config_for('B'),standard());self.assertEqual(self.w.selected_codes(),['B','A'])
        self.w.store_presets['A']['config']['parameters']['moq_days']=3;self.w.loaded_ok(loaded())
        self.assertEqual(self.w.config_for('A')['parameters']['moq_days'],3)
        snap=self.w.snapshot();pp.delete(self.w.work_repository,self.p['id'],'standard',pp.snapshot(self.w.work_repository))
        self.w.reset_job();self.w.restore_job({'day':wr.today(),'saved':'test','payload':snap})
        self.assertEqual(self.w.config_for('A')['parameters']['moq_days'],3);self.assertEqual(self.w.config_for('B'),standard())
    def test_new_plan_editor_order_selection_and_follow_default(self):
        from parameter_preset_ui import StorePlanEditor
        from PySide6.QtCore import Qt
        d=StorePlanEditor(self.w,self.plan)
        try:
            self.assertEqual([r['code'] for r in d.rows],['B','A']);self.assertEqual(d.table.cellWidget(1,2).currentData(),None)
            d.table.setCurrentCell(1,1);d.move(-1);self.assertEqual(d.rows[0]['code'],'A')
            d.batch.setCurrentIndex(d.batch.findData('standard'));d.set_selected();d.collect()
            self.assertTrue(all(r['preset_id']=='standard' for r in d.rows))
            d.check_visible(False);self.assertTrue(all(d.table.item(i,0).checkState()==Qt.CheckState.Unchecked for i in range(2)))
        finally:d.deleteLater()

    def test_ai_snapshot_resolves_plan_without_touching_current(self):
        # Use the same gateway initialized by a real application window below;
        # its snapshot method only relies on the existing workbench services.
        from application_window import ApplicationWindow
        w=ApplicationWindow()
        try:
            snapshot=w.automation.snapshot({'plan_id':self.plan['id']})
            self.assertEqual(snapshot['plan']['_resolved_presets']['A']['config']['warehouses']['山东仓'][0],6)
            self.assertEqual(snapshot['plan']['_resolved_presets']['B']['id'],self.p['id'])
            resources=w.automation.find_resources({'kind':'plans','limit':100})
            self.assertIn('per_store_presets',str(resources))
            self.assertIsNone(w.active_plan)
        finally:
            import shiboken6
            w._discard_close=True;w.close();self.app.processEvents();shiboken6.delete(w)


class ScheduledPresets(unittest.TestCase):
    def test_scheduler_uses_per_store_configs_and_detects_later_change(self):
        from test_scheduled_replenishment import fixture
        import scheduled_execution as se,batch_service
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);work,s,oldbasis,catalogs,manifest=fixture(root)
            oldbasis=se.freeze(root,s,'2026-11-05')
            c=standard();c['warehouses']['山东仓']=[3,2];p=pp.save(work,'短周期',c)
            plan=plans.Repository(work.path).save({'name':'新线路','config':standard(),'parameter_mode':pp.MODE,
                'stores':[{'code':'B','name':'乙','preset_id':p['id']},{'code':'A','name':'甲','preset_id':None}]})
            s['plan_id']=plan['id'];basis=se.freeze(root,s,'2026-11-05')
            self.assertEqual(list(basis['jobs']),['B','A'])
            self.assertEqual(basis['jobs']['B']['parameter_config']['warehouses']['山东仓'],[3,2])
            self.assertEqual(basis['jobs']['A']['parameter_config'],oldbasis['jobs']['A']['parameter_config'])
            loaded=batch_service.load([i['file'] for i in manifest['files']],catalogs)
            got=batch_service.calculate_store(loaded,'B',basis['jobs']['B'],basis['profile'])
            expected_settings=copy.deepcopy(oldbasis['jobs']['B']);expected_settings['parameter_config']=c;expected_settings['warehouses']=c['warehouses'];expected_settings['parameter_signature']=schema.signature(c)
            expected=batch_service.calculate_store(loaded,'B',expected_settings,basis['profile'])
            self.assertEqual(got['rows'],expected['rows']);self.assertEqual(got['summary'],expected['summary'])
            before=se.check_signature(basis);c['warehouses']['山东仓']=[12,5];pp.save(work,'长周期',c,p['id'])
            self.assertNotEqual(before,se.check_signature(se.freeze(root,s,'2026-11-05')))
            self.assertEqual(basis['jobs']['B']['parameter_config']['warehouses']['山东仓'],[3,2])


if __name__=='__main__':unittest.main()
