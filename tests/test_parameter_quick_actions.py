import copy,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from PySide6.QtWidgets import QApplication
from openpyxl import load_workbook
import parameter_tables as tables
import parameter_presets as presets
import parameter_schema as schema
import store_parameter_import as spi
import store_parameter_ui as ui
from parameter_quick_ui import QuickSettings
from parameter_preset_ui import commit_preset_selection,preset_summary
from test_store_parameter_ui import Host


class QuickActions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.host=Host(self.root/'db');self.host.touch=lambda:None
        self.a,self.b=spi.base(),spi.base()
        self.a['ui'].update(mode=2,amount='123',primary='山东仓')
        self.b['ui'].update(mode=1,primary='河北仓',secondary='山东仓')
        self.a['preset']['config']['warehouses']['山东仓']=[6,3]
        self.b['preset']['config']['warehouses']['山东仓']=[4,2]
        self.host.work_repository.save_stores({'A':self.a,'B':self.b})
        for code,r in [('A',self.a),('B',self.b)]:
            self.host.store_presets[code]=copy.deepcopy(r['preset']);self.host.uis[code]=copy.deepcopy(r['ui'])
    def tearDown(self):self.tmp.cleanup()
    def data(self,values,codes=('A','B')):
        return {'scoped':True,'sources':{},'rows':[{'code':s,'name':'','source':'界面测试','values':values} for s in codes]}
    def apply(self,values,scope='defaults',codes=('A','B')):
        d=self.data(values,codes);snapshot=ui.capture(self.host,d);p=ui.preview(d,snapshot,scope);self.assertFalse(p['errors']);ui.commit(self.host,d,snapshot,scope,p);return p
    def preset(self):
        c=schema.standard();c['warehouses']['山东仓']=[7,2];c['category_profiles']={'不得覆盖':'快销'}
        return presets.save(self.host.work_repository,'测试7天',c)
    def selection(self,p,save=False):
        data=presets.snapshot(self.host.work_repository);current=ui.capture(self.host,self.data({}))['current']
        commit_preset_selection(self.host,['A','B'],p,data,'current',current,save)
    def test_initial_no_change_and_no_first_store_copy(self):
        d=QuickSettings(None,2,'defaults');self.assertEqual(d.values(),{});self.assertFalse(d.amount.isEnabled());d.finish();self.assertIn('尚未选择',d.error.text());d.close()
    def test_dialog_budget_requires_value_and_zero_is_real(self):
        d=QuickSettings(None,2,'defaults');d.budget.setCurrentIndex(2)
        with self.assertRaisesRegex(ValueError,'需要填写'):d.values()
        d.amount.setText('0');self.assertEqual(d.values(),{('预算方式',):'限定预算',('预算金额',):0});d.budget.setCurrentIndex(1);self.assertNotIn(('预算金额',),d.values());d.close()
    def test_switch_only_keeps_differing_cycles_routes_budget(self):
        before=self.host.work_repository.stores();current=copy.deepcopy(self.host.store_presets)
        self.apply({('新品铺设',):'关闭'})
        after=self.host.work_repository.stores()
        for s in before:
            self.assertEqual(after[s]['ui'],before[s]['ui']);self.assertEqual(after[s]['preset']['config']['warehouses'],before[s]['preset']['config']['warehouses'])
            self.assertFalse(after[s]['preset']['config']['restock_switches']['new_stocking'])
        self.assertEqual(current,self.host.store_presets);self.assertFalse(self.host.invalidated)
    def test_selected_only_current_budget_zero(self):
        saved=self.host.work_repository.stores();b=copy.deepcopy(self.host.uis['B'])
        self.apply({('预算金额',):0},'current',('A',));self.assertEqual(self.host.uis['A']['amount'],'0');self.assertEqual(self.host.uis['B'],b);self.assertEqual(saved,self.host.work_repository.stores());self.assertEqual(self.host.invalidated,['A'])
    def test_single_warehouse_clears_secondary_keeps_cycles(self):
        self.apply({('主仓',):'山东仓',('从仓',):'无从仓'})
        saved=self.host.work_repository.stores();self.assertIsNone(saved['B']['ui']['secondary']);self.assertEqual(saved['B']['preset']['config']['warehouses']['山东仓'],[4,2])
    def test_budget_conditional_blank_keeps_existing_but_missing_fails(self):
        data=self.data({('预算方式',):'限定预算'})
        p=ui.preview(data,ui.capture(self.host,data),'defaults');self.assertEqual(p['values']['A']['ui']['amount'],'123');self.assertEqual(len(p['errors']),1);self.assertIn('预算金额',p['errors'][0])
    def test_budget_conflict_and_nonfinite_rejected(self):
        for fields in [{('预算方式',):'不限预算',('预算金额',):2},{('预算金额',):'NaN'},{('预算金额',):-1}]:
            d=self.data(fields);p=ui.preview(d,ui.capture(self.host,d),'defaults');self.assertTrue(p['errors'])
    def test_apply_existing_preset_current_keeps_routes_budget_mapping_defaults(self):
        p=self.preset();before=self.host.work_repository.stores();uis=copy.deepcopy(self.host.uis);mapping=copy.deepcopy(self.host.config_for('A')['category_profiles'])
        self.selection(p)
        self.assertEqual(self.host.uis,uis);self.assertEqual(self.host.config_for('A')['category_profiles'],mapping);self.assertEqual(before,self.host.work_repository.stores());self.assertEqual(self.host.invalidated,['A','B']);self.assertEqual(self.host.preset_for('A')['id'],p['id'])
    def test_apply_and_save_preserves_separate_default_and_current_settings(self):
        p=self.preset();self.host.uis['A']['amount']='456';self.selection(p,True)
        saved=self.host.work_repository.stores();self.assertEqual(saved['A']['ui']['amount'],'123');self.assertEqual(self.host.uis['A']['amount'],'456');self.assertEqual(saved['A']['preset']['id'],p['id'])
    def test_apply_defaults_only_not_current(self):
        p=self.preset();before=copy.deepcopy(self.host.store_presets)
        commit_preset_selection(self.host,['A'],p,presets.snapshot(self.host.work_repository),'defaults',{})
        self.assertEqual(self.host.store_presets,before);self.assertEqual(self.host.work_repository.stores()['A']['preset']['id'],p['id']);self.assertNotEqual(self.host.work_repository.stores()['B']['preset']['id'],p['id'])
    def test_stale_current_and_preset_rejected_before_save(self):
        p=self.preset();data=presets.snapshot(self.host.work_repository);current=ui.capture(self.host,self.data({}))['current']
        self.host.uis['A']['amount']='222'
        with self.assertRaisesRegex(ValueError,'本次门店设置'):commit_preset_selection(self.host,['A','B'],p,data,'current',current,True)
        current=ui.capture(self.host,self.data({}))['current'];presets.save(self.host.work_repository,'另一个',schema.standard())
        with self.assertRaises(Exception):commit_preset_selection(self.host,['A','B'],p,data,'current',current,True)
        self.assertFalse(self.host.invalidated);self.assertNotEqual(self.host.work_repository.stores()['A']['preset']['id'],p['id'])
    def test_save_failure_does_not_apply_current(self):
        p=self.preset();before=copy.deepcopy(self.host.store_presets)
        with patch.object(presets,'assign',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.selection(p,True)
        self.assertEqual(before,self.host.store_presets);self.assertFalse(self.host.invalidated)
    def test_readonly_and_running_block_current(self):
        p=self.preset()
        self.host.readonly=True
        with self.assertRaises(ValueError):self.selection(p)
        self.host.readonly=False;self.host.worker=object()
        with self.assertRaises(ValueError):self.selection(p)
    def test_summary_exposes_actual_values(self):
        text=preset_summary(self.preset());self.assertIn('备货 7 天，到货 2 天',text);self.assertIn('库存目标分位概率',text);self.assertIn('新品',text)
    def test_mapping_incompatible_blocks_before_any_default_or_current_write(self):
        p=self.preset();self.host.store_presets['A']['config']['weight_profiles']['自定义']=copy.deepcopy(schema.standard()['weight_profiles']['快销'])
        self.host.store_presets['A']['config']['category_profiles']['自定义类']='自定义'
        before=self.host.work_repository.stores();current=copy.deepcopy(self.host.store_presets)
        with self.assertRaisesRegex(ValueError,'分类映射不兼容'):self.selection(p,True)
        self.assertEqual(before,self.host.work_repository.stores());self.assertEqual(current,self.host.store_presets)
    def test_identical_config_updates_selection_without_invalidating_result(self):
        p=presets.save(self.host.work_repository,'相同值',self.host.config_for('A'))
        data=presets.snapshot(self.host.work_repository);current=ui.capture(self.host,self.data({},('A',)))['current']
        commit_preset_selection(self.host,['A'],p,data,'current',current)
        self.assertFalse(self.host.invalidated);self.assertEqual(self.host.preset_for('A')['id'],p['id'])
    def test_templates_dropdown_required_comments_and_parser_headers_unchanged(self):
        for kind in tables.KINDS:
            path=self.root/(kind+'.xlsx');tables.write(path,kind,scoped=True)
            w=load_workbook(path)
            for title in tables.KINDS[kind]:
                ws=w[title];self.assertEqual([c.value for c in ws[1]],tables.SHEETS[title]);self.assertIn('必填',ws['A1'].comment.text)
                for dv in ws.data_validations.dataValidation:self.assertFalse(dv.showDropDown);self.assertTrue(dv.showInputMessage)
                if title=='门店预算':self.assertTrue(any('不限预算' in (d.formula1 or '') for d in ws.data_validations.dataValidation))
            w.close()
    def test_v3_blank_keeps_and_v2_blank_resets(self):
        for scoped in (True,False):
            path=self.root/('v3.xlsx' if scoped else 'v2.xlsx');tables.write(path,scoped=scoped)
            w=load_workbook(path)
            for col,value in enumerate(['A','','山东仓',None,None],1):w['分仓周期'].cell(2,col,value)
            w.save(path);w.close()
            p=spi.plan(spi.read([path]),{'A':self.a});self.assertFalse(p['errors']);self.assertEqual(p['values']['A']['preset']['config']['warehouses']['山东仓'],[6,3] if scoped else [8,5])
    def test_template_bad_paste_rejected_with_source(self):
        path=self.root/'预算.xlsx';tables.write(path,'store_budget',scoped=True)
        w=load_workbook(path)
        for col,value in enumerate(['A','','随便填',3],1):w['门店预算'].cell(2,col,value)
        w.save(path);w.close()
        p=spi.plan(spi.read([path]),{'A':self.a});self.assertIn('第2行',p['errors'][0]);self.assertIn('预算方式',p['errors'][0])

if __name__=='__main__':unittest.main()
