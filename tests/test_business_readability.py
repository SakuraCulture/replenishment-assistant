"""Display-only contracts: immutable values, actual stages, old snapshot gaps."""
import copy, sys, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import business_presenter as view
import parameter_schema

def goods():
    return {'D':'测试商品','E':'001','G':'包','AU':'河北仓','U':'快销','J':60,'K':0,'L':0,
            'AG':8,'AH':5,'AB':.4,'AC':.6,'AD':.2,'AE':.2,'AS':24,'_base_qty':0,
            '_candidate_qty':24,'_candidate_amount':48,'_allocated_qty':12,'_allocated_amount':24,
            '_budget_active':True,'_budget_gap':12,'_budget_reason':'预算限量',
            '_source':'资料匹配','_meta':{'blocking':[]},'_audit':{'applied':True,'reason':'一箱恢复'}}

class ReadabilityTests(unittest.TestCase):
    def test_all_model_columns_have_business_names(self):
        from archive_exports import LETTER_LABELS
        for key in LETTER_LABELS:self.assertNotEqual(view.title(key),key)
        self.assertEqual(view.title('AB'),'趋势敏感度')
        self.assertEqual(view.title('AC'),'最近7天权重')
    def test_null_zero_blank_and_unlimited_are_distinct(self):
        self.assertEqual(view.value_text(None,'budget'),'不限预算')
        self.assertEqual(view.value_text(0,'budget'),'0')
        self.assertEqual(view.value_text(None,'V',missing='缺价'),'缺价')
        self.assertEqual(view.value_text(''),'空白')
        self.assertEqual(view.value_text(.9,'probability'),'90%')
        self.assertEqual(view.value_text(.905,'probability'),'90.5%')
    def test_stages_keep_actual_values_and_input_unchanged(self):
        d=goods();old=copy.deepcopy(d);rows=view.stage_rows(d)
        self.assertEqual([r[1] for r in rows],['0','24','24','12','12'])
        self.assertEqual(d,old);self.assertIn('-12包',rows[3][3])
    def test_manual_uses_original_candidate_not_overwritten_field(self):
        d=goods();d.update(_candidate_qty=48,_allocated_qty=48)
        d['_preissue']={'automatic_quantity':12,'quantity':48,'delta':36,'reason':'活动',
            'original_fields':{'_candidate_qty':{'present':True,'value':24}}}
        self.assertEqual([r[1] for r in view.stage_rows(d)],['0','24','24','12','48'])
    def test_missing_stage_is_unknown_not_zero(self):
        d=goods();del d['_base_qty'];del d['_candidate_qty']
        rows=view.stage_rows(d)
        self.assertEqual(rows[0][1],view.MISSING);self.assertEqual(rows[2][1],view.MISSING)
        self.assertNotIn('较上一阶段',rows[3][3])
    def test_actual_category_parameter_and_warehouse_do_not_use_defaults(self):
        d=goods();c=parameter_schema.standard();c['profile_parameters']={'快销':{'probability':.95}}
        r={'effective_settings':{'parameter_config':c,'budget':0},'enabled':False}
        old=copy.deepcopy((r,d));rows={x[0]:x for x in view.parameter_rows(r,d)}
        self.assertEqual(rows['库存目标分位概率'][1],'95%');self.assertEqual(rows['到货天数'][1],'5')
        self.assertEqual(rows['门店预算'][1],'0');self.assertEqual((r,d),old)
        self.assertEqual({x[0]:x for x in view.parameter_rows({},d)}['门店预算'][1],view.MISSING)
    def test_unknown_technical_fields_are_not_lost(self):
        rows=view.flat_rows({'future':{'x':None},'items':[]},technical=True)
        self.assertEqual(len(rows),2);self.assertIn('future / x',rows[0][3]);self.assertEqual(rows[0][1],'未提供')
    def test_original_export_names_unchanged(self):
        from archive_exports import LETTER_LABELS
        from history_forms import LABELS
        self.assertEqual(LETTER_LABELS['AB'],'α');self.assertEqual(LABELS['_base_qty'],'例外前建议量')
        self.assertEqual(view.title('_base_qty'),'原模型建议量')
    def test_form_does_not_cross_warehouse(self):
        d=goods();order={'warehouse':'山东仓','payload':{'result':{'rows':[d]}}}
        self.assertIn('请选择',view.historical(None,order,'决策总览','001')[0][1])

if __name__=='__main__':unittest.main()
