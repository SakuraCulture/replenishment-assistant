import copy,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from openpyxl import Workbook,load_workbook
import parameter_schema as schema
import parameter_tables as tables
import store_parameter_import as spi
import system_standard

class ClassifiedParameters(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.base=spi.base();self.base['preset']['config']['parameters']['probability']=.95
    def tearDown(self):self.tmp.cleanup()
    def book(self,sheets,name='input.xlsx',marker=True):
        w=Workbook();w.remove(w.active)
        for title,rows in sheets.items():
            s=w.create_sheet(title)
            for row in rows:s.append(row)
        if marker:w.create_sheet(tables.MARKER).append([tables.FORMAT])
        p=self.root/name;w.save(p);w.close();return p
    def apply(self,sheets,**kwargs):
        data=spi.read([self.book(sheets,**kwargs)])
        plan=spi.plan(data,{'001':self.base})
        self.assertEqual(plan['errors'],[])
        return plan['values']['001'],data
    def test_warehouse_rows_no_route_or_budget(self):
        value,data=self.apply({'分仓周期':[tables.SHEETS['分仓周期'],['001','','山东仓',6,3],['001','','河北仓',5,2]]})
        self.assertEqual(value['preset']['config']['warehouses'],{'山东仓':[6,3],'河北仓':[5,2]})
        self.assertEqual(value['ui'],self.base['ui']);self.assertEqual(len(data['rows']),1)
    def test_blank_defaults_missing_column_untouched_and_zero(self):
        self.base['preset']['config']['warehouses']['山东仓']=[15,9]
        value,data=self.apply({'分仓周期':[['门店编码','属仓','备货天数'],['001','山东仓',None]]})
        self.assertEqual(value['preset']['config']['warehouses']['山东仓'],[8,9])
        self.assertEqual(data['rows'][0]['reset_fields'],['备货天数'])
        value,_=self.apply({'分仓周期':[['门店编码','属仓','备货天数'],['001','山东仓',0]]},name='zero.xlsx')
        self.assertEqual(value['preset']['config']['warehouses']['山东仓'],[0,9])
    def test_legacy_blank_unchanged(self):
        value,data=self.apply({'门店参数':[['门店编码','库存目标分位概率'],['001',None]]},marker=False)
        self.assertEqual(value['preset']['config']['parameters']['probability'],.95)
        self.assertTrue(data['formats'][0].startswith('旧模板'))
    def test_shared_category_override_inherits_and_percent(self):
        self.base['preset']['config']['profile_parameters']={'快销':{'probability':.99}}
        value,_=self.apply({'分类参数':[['门店编码','分类组','库存目标分位概率','最近7天权重'],['001','快销',None,'65%']]})
        c=value['preset']['config'];self.assertNotIn('profile_parameters',c)
        self.assertEqual(c['weight_profiles']['快销']['w1'],.65)
        self.assertEqual(schema.profile_values(c,'快销')['probability'],.95)
    def test_default_blank_probability(self):
        value,_=self.apply({'门店通用参数':[['门店编码','库存目标分位概率'],['001',None]]})
        self.assertEqual(value['preset']['config']['parameters']['probability'],.9)
    def test_invalid_no_commit(self):
        p=self.book({'门店通用参数':[['门店编码','库存目标分位概率'],['001',0]]})
        original=copy.deepcopy(self.base);plan=spi.plan(spi.read([p]),{'001':self.base})
        self.assertTrue(plan['errors']);self.assertEqual(self.base,original)
    def test_duplicate_conflict_rejected(self):
        p=self.book({'分仓周期':[['门店编码','属仓','备货天数'],['001','山东仓',6],['001','山东仓',7]]})
        with self.assertRaisesRegex(ValueError,'冲突'):spi.read([p])
    def test_identical_duplicates_trace(self):
        p=self.book({'分仓周期':[['门店编码','属仓','备货天数'],['001','山东仓',6],['001','山东仓',6]]})
        data=spi.read([p]);self.assertEqual(len(data['rows']),1);self.assertEqual(len(data['rows'][0]['duplicates']),1)
    def test_formula_long_numeric_and_duplicate_header(self):
        for i,rows in enumerate([
          [['门店编码','属仓','备货天数'],['001','山东仓','=1+1']],
          [['门店编码','属仓'],[12345678901234567,'山东仓']],
          [['门店编码','属仓','属仓'],['001','山东仓','山东仓']]]):
            with self.subTest(i=i),self.assertRaises(ValueError):spi.read([self.book({'分仓周期':rows},name=str(i)+'.xlsx')])
    def test_complete_roundtrip_and_template_scope(self):
        p=self.root/'export.xlsx';tables.write(p,'classified_parameters',{'001':self.base})
        plan=spi.plan(spi.read([p]),{'001':self.base});self.assertEqual(plan['errors'],[]);self.assertEqual(plan['changed'],[])
        w=load_workbook(p);self.assertEqual(w[tables.MARKER].sheet_state,'hidden');self.assertNotIn('预算金额',[c.value for c in w['分仓周期'][1]]);w.close()
    def test_restore_not_mutating_standard_or_store(self):
        c=copy.deepcopy(self.base['preset']['config']);r=system_standard.restore(c,'parameters','probability')
        self.assertEqual(r['parameters']['probability'],.9);self.assertEqual(c['parameters']['probability'],.95)
        r['parameters']['probability']=.5;self.assertEqual(schema.standard()['parameters']['probability'],.9)
    def test_restore_category_explicit_default_not_custom_parent(self):
        r=system_standard.restore(self.base['preset']['config'],'profile_parameters',('快销','probability'))
        self.assertEqual(schema.profile_values(r,'快销')['probability'],.9)
    def test_standard_integrity_error_not_silent_fallback(self):
        with patch.object(system_standard,'EXPECTED','broken'),self.assertRaisesRegex(ValueError,'完整性'):schema.standard()

if __name__=='__main__':unittest.main()
