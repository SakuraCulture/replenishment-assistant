import copy,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import parameter_maintenance as pm
import parameter_schema as schema
import parameter_tables as tables
import store_parameter_import as spi
import work_repository as wr
from openpyxl import load_workbook,Workbook


class ParameterWorkspaces(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.repo=wr.Repository(self.root/'settings')
        self.a=spi.base();self.b=spi.base();self.a['preset']['config']['parameters']['probability']=.95
        self.b['preset']['config']['warehouses']['山东仓']=[4,2]
        self.b['preset']['config']['weight_profiles']['快销']['alpha']=.2
        self.repo.save_stores({'001':self.a,'002':self.b})
    def tearDown(self):self.tmp.cleanup()
    def test_patch_only_selected_fields_preserves_distinct_stores(self):
        before=copy.deepcopy([self.a,self.b]);values={('warehouses','河北仓',0):6}
        for r in (self.a,self.b):
            new=pm.patch(r['preset']['config'],values);expected=copy.deepcopy(r['preset']['config']);expected['warehouses']['河北仓']=list(expected['warehouses']['河北仓']);expected['warehouses']['河北仓'][0]=6
            self.assertEqual(new,expected)
        self.assertEqual([self.a,self.b],before)
    def test_common_reset_preserves_category_override_and_cycles(self):
        c=self.a['preset']['config'];c['profile_parameters']={'日百':{'probability':.99}}
        after=pm.patch(c,{f:pm.default_value(f) for f in pm.fields([c],'common')})
        self.assertTrue(pm.common_is_default(after));self.assertEqual(after['profile_parameters'],c['profile_parameters']);self.assertEqual(after['warehouses'],c['warehouses'])
        self.assertEqual(schema.profile_values(after,'日百')['probability'],.99)
    def test_category_inheritance_uses_each_stores_common(self):
        for r,p in [(self.a,.95),(self.b,.9)]:
            c=copy.deepcopy(r['preset']['config']);c['profile_parameters']={'快销':{'probability':.8}}
            new=pm.patch(c,{('profile_parameters','快销','probability'):'沿用门店'})
            self.assertEqual(schema.profile_values(new,'快销')['probability'],p)
    def test_invalid_patch_rejects_all_and_protects_other_groups(self):
        for changes in [{('parameters','probability'):0},{('category_profiles','饮料'):'快销'},{('exception',):False}]:
            with self.assertRaises(ValueError):pm.patch(self.a['preset']['config'],changes)
    def test_shared_mapping_does_not_rewrite_stores_and_snapshot(self):
        original=copy.deepcopy(self.a);before=self.repo.stores();snap=pm.mapping_snapshot(self.repo.path);mapping={'测试分类':'快销'}
        pm.save_mapping(self.repo.path,mapping,snap);new=pm.mapping_snapshot(self.repo.path)
        p=pm.bind(self.a['preset'],new);self.assertEqual(p['config']['category_profiles'],mapping)
        self.assertEqual(self.repo.stores(),before);self.assertEqual(self.a,original)
        pm.save_mapping(self.repo.path,{'测试分类':'日百'},new)
        self.assertEqual(p['config']['category_profiles'],mapping)
    def test_mapping_conflict_requires_explicit_selection(self):
        self.b['preset']['config']['category_profiles']={'特殊':'日百'};self.repo.save_stores({'002':self.b})
        snap=pm.mapping_snapshot(self.repo.path);self.assertTrue(snap['conflict'])
        with self.assertRaisesRegex(ValueError,'不一致'):pm.bind(self.a['preset'],snap)
        pm.save_mapping(self.repo.path,{'特殊':'日百'},snap)
        self.assertFalse(pm.mapping_snapshot(self.repo.path)['conflict'])
    def test_mapping_revision_and_store_concurrent_change(self):
        snap=pm.mapping_snapshot(self.repo.path);pm.save_mapping(self.repo.path,{},snap)
        with self.assertRaises(wr.Conflict):pm.save_mapping(self.repo.path,{},snap)
        snap=pm.mapping_snapshot(self.repo.path);self.a['preset']['config']['parameters']['probability']=.8;self.repo.save_stores({'001':self.a})
        with self.assertRaises(wr.Conflict):pm.save_mapping(self.repo.path,{},snap)
    def test_mapping_invalid_group_and_cancellation(self):
        import threading
        from runtime_support import Cancelled
        snap=pm.mapping_snapshot(self.repo.path)
        with self.assertRaises(ValueError):pm.save_mapping(self.repo.path,{'药':'不存在'},snap)
        cancel=threading.Event();cancel.set()
        with self.assertRaises(Cancelled):pm.save_mapping(self.repo.path,{},snap,cancel=cancel)
        self.assertIsNone(pm.mapping_snapshot(self.repo.path)['current'])
    def test_new_template_only_two_parameter_sheets(self):
        from import_templates import write as create
        p=self.root/'new.xlsx';create('classified_parameters',p)
        b=load_workbook(p);self.assertEqual([s for s in b.sheetnames if s in tables.SHEETS],['分仓周期','分类参数']);self.assertEqual(b[tables.MARKER]['A1'].value,tables.FORMAT_SCOPED);b.close()
    def test_v3_blank_keep_zero_real_and_explicit_default(self):
        p=self.root/'v3.xlsx';b=Workbook();s=b.active;s.title='分仓周期';s.append(['属仓','到货天数','门店编码','备货天数']);s.append(['山东仓',None,'002',0]);b.create_sheet(tables.MARKER).append([tables.FORMAT_SCOPED]);b.save(p);b.close()
        data=tables.read([p]);self.assertTrue(data['scoped']);plan=spi.plan(data,{'002':self.b});self.assertEqual(plan['errors'],[]);self.assertEqual(plan['values']['002']['preset']['config']['warehouses']['山东仓'],[0,2])
        b=load_workbook(p);b['分仓周期']['D2']='系统默认';b.save(p);b.close();plan=spi.plan(tables.read([p]),{'002':self.b});self.assertEqual(plan['values']['002']['preset']['config']['warehouses']['山东仓'],[8,2])
    def test_v3_roundtrip_clears_category_override_explicitly(self):
        p=self.root/'out.xlsx';tables.write(p,'classified_parameters',{'001':self.a},scoped=True)
        old=copy.deepcopy(self.a);old['preset']['config']['profile_parameters']={'快销':{'probability':.99}}
        result=spi.plan(tables.read([p]),{'001':old});self.assertEqual(result['errors'],[]);self.assertEqual(result['values']['001']['preset']['config'],self.a['preset']['config'])
    def test_common_template_separate_and_mapping_no_store(self):
        from import_templates import write as create
        from global_mapping_io import write,read
        p=self.root/'common.xlsx';create('common_parameters',p);b=load_workbook(p);self.assertIn('门店通用参数',b.sheetnames);self.assertNotIn('分类参数',b.sheetnames);b.close()
        p=self.root/'mapping.xlsx';write(p,{'001分类':'日百'});self.assertEqual(read(p),{'001分类':'日百'})
        b=load_workbook(p);self.assertNotIn('门店编码',[c.value for c in b.active[1]]);b.active['B2']='=1';b.save(p);b.close()
        with self.assertRaises(ValueError):read(p)
    def test_plan_selective_values_not_mapping_or_unselected_common(self):
        import replenishment_plans as plans
        c=schema.standard();c['warehouses']['山东仓']=[7,3];c['category_profiles']={'旧映射':'日百'}
        plan={'name':'青岛线','stores':[{'code':'001','name':'甲'}], 'config':c,'parameter_fields':[['warehouses','山东仓',0]]}
        plans.validate(plan);r=pm.plan_config(plan,self.a['preset']['config'])
        self.assertEqual(r['parameters']['probability'],.95);self.assertEqual(r['warehouses']['山东仓'],[7,5]);self.assertEqual(r['category_profiles'],self.a['preset']['config']['category_profiles'])
        saved=plans.Repository(self.repo.path).save(plan);self.assertEqual(plans.Repository(self.repo.path).list()[0],saved)
    def test_legacy_plan_keeps_parameter_values_but_uses_mapping_snapshot(self):
        c=schema.standard();c['parameters']['probability']=.8;c['category_profiles']={'历史':'快销'}
        new=pm.plan_config({'config':c},self.a['preset']['config']);self.assertEqual(new['parameters']['probability'],.8);self.assertEqual(new['category_profiles'],self.a['preset']['config']['category_profiles'])
    def test_apply_both_patches_current_and_defaults_separately(self):
        import store_parameter_ui as ui
        current=copy.deepcopy(self.a);current['preset']['config']['parameters']['probability']=.7
        current['ui']['mode']=2;current['ui']['amount']='50'
        data=pm.selection_data(['001'],{('warehouses','山东仓',0):6});snap={'saved':{'001':self.a},'current':{'001':current},'names':{}}
        result=ui.preview(data,snap,'both');self.assertEqual(result['errors'],[])
        self.assertEqual(result['values']['001']['preset']['config']['parameters']['probability'],.7)
        saved=result['saved_plan']['values']['001'];self.assertEqual(saved['preset']['config']['parameters']['probability'],.95);self.assertEqual(saved['ui'],self.a['ui'])
    def test_qt_mixed_values_and_explicit_default(self):
        from PySide6.QtWidgets import QApplication
        from parameter_maintenance_ui import PatchDialog
        app=QApplication.instance() or QApplication([])
        d=PatchDialog(None,{'001':self.a['preset']['config'],'002':self.b['preset']['config']})
        f=('warehouses','山东仓',0);i=d.fields.index(f);self.assertIn('多种值',d.table.item(i,2).text())
        for chk,_,_ in d.controls.values():self.assertFalse(chk.isChecked())
        chk,mode,edit=d.controls[f];chk.setChecked(True);edit.setText('6');d.finish();self.assertEqual(d.changes,{f:6});d.deleteLater();app.processEvents()

    def test_inherited_category_displays_effective_common_values(self):
        from PySide6.QtWidgets import QApplication
        from parameter_maintenance_ui import PatchDialog
        app=QApplication.instance() or QApplication([])
        d=PatchDialog(None,{'001':self.a['preset']['config'],'002':self.b['preset']['config']})
        i=d.fields.index(('profile_parameters','快销','probability'));cell=d.table.item(i,2)
        self.assertIn('多种值',cell.text());self.assertIn('95%',cell.toolTip());self.assertIn('90%',cell.toolTip());self.assertIn('沿用门店通用值',cell.toolTip())
        d.deleteLater();app.processEvents()

class ParameterSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        from workbench import Workbench
        self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,REPLENISHMENT_DATA_DIR=self.tmp.name);self.env.start();self.win=Workbench()
    def tearDown(self):
        import shiboken6
        self.win._discard_close=True;self.win.close();self.app.processEvents();shiboken6.delete(self.win);self.env.stop();self.tmp.cleanup()
    def test_new_job_resolves_mapping_history_retains_frozen_values(self):
        from test_parameter_reload import loaded,record
        w=self.win;w.work_repository.save_stores({'A':record()});snap=pm.mapping_snapshot(w.work_repository.path)
        pm.save_mapping(w.work_repository.path,{'休闲食品':'日百'},snap);w.loaded_ok(loaded())
        self.assertEqual(w.config_for('A')['category_profiles'],{'休闲食品':'日百'})
        self.assertEqual(w.config_for('B')['category_profiles'],{'休闲食品':'日百'})
        payload=copy.deepcopy(w.snapshot());settings=copy.deepcopy(w.selected_settings('A'))
        pm.save_mapping(w.work_repository.path,{'休闲食品':'快销'},pm.mapping_snapshot(w.work_repository.path))
        self.assertEqual(w.selected_settings('A'),settings)
        w.restore_job({'day':wr.today(),'saved':wr.now(),'payload':payload})
        self.assertEqual(w.config_for('A')['category_profiles'],{'休闲食品':'日百'})
        w.reset_job();w.loaded_ok(loaded());self.assertEqual(w.config_for('A')['category_profiles'],{'休闲食品':'快销'})
    def test_plan_selected_fields_keep_store_differences_and_calculation_uses_them(self):
        from test_parameter_reload import loaded,record
        import replenishment_plans as plans
        w=self.win;a=record(6);b=record(9);b['preset']['config']['parameters']['probability']=.7
        w.work_repository.save_stores({'A':a,'B':b});w.loaded_ok(loaded())
        c=schema.standard();c['warehouses']['河北仓']=[4,2]
        plan=plans.Repository(w.work_repository.path).save({'name':'测试线','stores':[{'code':'B','name':'测试B'},{'code':'A','name':'测试A'}],'config':c,'parameter_fields':[['warehouses','河北仓',0]]})
        plans.apply(w,plan)
        self.assertEqual(list(w.loaded['stores']),['B','A'])
        for code,depth,prob in [('A',6,.9),('B',9,.7)]:
            settings=w.selected_settings(code)['parameter_config'];self.assertEqual(settings['warehouses']['山东仓'],[depth,3.5]);self.assertEqual(settings['warehouses']['河北仓'][0],4);self.assertEqual(settings['parameters']['probability'],prob)
    def test_conflicting_mapping_never_silently_uses_first_store(self):
        from test_parameter_reload import loaded,record
        w=self.win;a=record();b=record();b['preset']['config']['category_profiles']={'休闲食品':'日百'}
        w.work_repository.save_stores({'A':a,'B':b});w.loaded_ok(loaded())
        with self.assertRaisesRegex(ValueError,'分类映射'):w.selected_settings('A')

if __name__=='__main__':unittest.main()
