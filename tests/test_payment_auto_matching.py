"""Automatic recognition never needs a company/name dropdown for exact evidence."""
import copy,json,sys,threading,unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import openpyxl
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/p) for p in ('src','tests')]
import test_payment_matching as fixture
import payment_matching as pm,payment_auto_matching as automatic,lifecycle_batch as batch,lifecycle_io as io,order_lifecycle as lc,jst_orders as jst,work_repository as wr
from runtime_support import Cancelled

class PaymentAutoTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.PaymentMatchingTests();self.f.setUp();self.path=self.f.path;self.target=self.f.target
    def tearDown(self):self.f.tearDown()
    def analyze(self,d,**kwargs):return pm.analyze(self.path,d,pm.build(self.path),**kwargs)
    def prepare(self,docs,**kwargs):return pm.prepare(self.path,docs,None,pm.build(self.path),None,**kwargs)
    def explicit(self,number=None,header='签发单号',**kwargs):
        return self.f.document(headers=io.PAYMENT_HEADERS+[header],change=lambda v:v.update({header:number or self.target['number']}),**kwargs)
    def test_default_normal_alias_and_both_warehouses_without_manual_fields(self):
        docs=[self.f.document(name='门店名称有细微差别'),self.f.document(jst.get(self.path,self.f.f.gids[-1]),internal='SECOND',name='另一企业门店别称')]
        self.f.accounts_file.unlink()
        for d in docs:self.assertTrue(self.analyze(d)['automatic'])
        plan=self.prepare(docs);self.assertEqual(len(plan['plans']),2);self.assertTrue(all(r['automatic_proof']=='full_detail' for r in plan['payment_matching']['requests']))
        receipt=lc.commit(self.path,plan);self.assertEqual(len(receipt),2)
        with closing(pm.readonly(self.path)) as db:values=pm.mappings(db)
        self.assertEqual(len(values),2);self.assertTrue(all(v['kind']=='local_document' and v['company_inferred'] is False for v in values.values()));self.assertEqual(pm.aliases(self.path),[])
    def test_new_alias_does_not_need_prior_alias_save(self):
        for internal in ('NEW1','NEW2'):
            r=self.analyze(self.f.document(internal=internal,name='另一个名称'))
            self.assertTrue(r['automatic']);self.assertEqual(r['candidates'][0]['number'],self.target['number'])
    def test_reimport_and_one_cent_correction_without_source_selection(self):
        doc=self.f.document();first=lc.commit(self.path,self.prepare([doc]))[0]
        repeat=lc.commit(self.path,self.prepare([doc]))[0];self.assertFalse(repeat['changed']);self.assertEqual(first['version'],repeat['version'])
        correction=self.f.document(change=lambda v:v.update(商品金额=str(pm.Decimal(v['商品金额'])+pm.Decimal('.01'))))
        r=self.analyze(correction);self.assertTrue(r['automatic']);self.assertFalse(r['exact']);self.assertEqual(r['proof'],'previous_source');self.assertIn('+0.01',r['reason'])
        self.assertEqual(lc.commit(self.path,self.prepare([correction]))[0]['version'],first['version']+1)
    def test_first_amount_change_without_original_link_is_not_a_complete_match(self):
        d=self.f.document(change=lambda v:v.update(商品金额='121'))
        r=self.analyze(d);self.assertFalse(r['automatic']);self.assertIn('金额不同',r['reason'])
    def test_canonical_and_alias_appended_signing_number_are_read_and_automatic(self):
        for header in ('补货单号','签发单号'):
            d=self.explicit(header=header);self.assertTrue(all(r['signed_number']==self.target['number'] for r in d['scan']['rows']))
            self.assertTrue(self.analyze(d)['automatic']);self.assertEqual(self.prepare([d])['plans'][0]['head']['number'],self.target['number'])
    def test_duplicate_identity_headers_never_choose_first(self):
        for extra in (['签发单号','补货单号'],['签发单号','签发单号']):
            with self.assertRaisesRegex(ValueError,'同义表头'):io.payment_specs({'sheets':[{'headers':io.PAYMENT_HEADERS+extra}]})
    def test_wrong_explicit_number_blocks_instead_of_rewriting_or_silently_ignoring(self):
        other=next(h for h in self.f.index['heads'] if h['store']!=self.target['store'])
        d=self.explicit(other['number']);r=self.analyze(d)
        self.assertFalse(r['automatic']);self.assertTrue(r['prevent_rebind']);self.assertIn('签发单号与明细冲突',r['state']);self.assertIn(self.target['number'],r['reason'])
        d['target']=other
        with self.assertRaisesRegex(ValueError,'填写单号属于'):self.prepare([d])
        self.assertTrue(self.f.f.unpaid())
    def test_explicit_correct_identity_allows_real_payment_differences_in_review(self):
        d=self.explicit();d['scan']['rows'][0]['amount']='121'
        r=self.analyze(d);self.assertTrue(r['automatic']);self.assertFalse(r['exact']);self.assertEqual(r['proof'],'explicit_number')
    def test_same_day_scope_does_not_hide_cross_day_complete_collision(self):
        d=self.f.document();self.f.duplicate_previous_day();r=self.analyze(d,scope=[self.target])
        self.assertFalse(r['automatic']);self.assertEqual(len(r['exact_numbers']),2)
    def test_selected_scope_cannot_redirect_unique_other_order(self):
        other=next(h for h in self.f.index['heads'] if h['number']!=self.target['number'])
        r=self.analyze(self.f.document(),scope=[other]);self.assertFalse(r['automatic']);self.assertTrue(r['prevent_rebind']);self.assertIn('超出',r['state'])
    def test_missing_wrong_or_duplicate_sku_never_satisfies_full_match(self):
        d=self.f.document();bad=copy.deepcopy(d);bad['scan']['rows'][0]['sku']='WRONG-SKU'
        r=self.analyze(bad);self.assertFalse(r['automatic']);self.assertIn('清单外编码 WRONG-SKU',r['reason']);self.assertIn('少 1 款、多 1 款',r['reason'])
        duplicate=copy.deepcopy(d);duplicate['scan']['rows'].append(copy.deepcopy(d['scan']['rows'][0]));self.assertFalse(self.analyze(duplicate)['automatic'])
    def test_local_link_keeps_deleted_identity_and_cannot_rebind(self):
        import archive_orders as ao
        doc=self.f.document();lc.commit(self.path,self.prepare([doc]));ao.change_many(self.path,[self.target],True)
        r=self.analyze(doc);self.assertFalse(r['automatic']);self.assertTrue(r['prevent_rebind']);self.assertIn('原订单已失效',r['state'])
    def test_known_company_link_still_cannot_be_bypassed_by_wrong_explicit_number(self):
        d=self.f.document();lc.commit(self.path,self.f.plan(d));other=next(h for h in self.f.index['heads'] if h['store']!=self.target['store'])
        r=self.analyze(self.explicit(other['number']));self.assertTrue(r['prevent_rebind']);self.assertIn('原关联冲突',r['state'])
    def test_wrong_explicit_company_is_not_saved_even_with_manual_target(self):
        d=self.f.document();d['target']=self.target
        with self.assertRaisesRegex(ValueError,'供货仓不一致'):pm.prepare(self.path,[d],None,pm.build(self.path),'200')
        r=self.analyze(self.explicit(),source_scope='200');self.assertFalse(r['automatic']);self.assertIn('供货仓不一致',r['state'])
    def test_manual_target_cannot_override_automatic_or_previous_relation(self):
        d=self.f.document();lc.commit(self.path,self.prepare([d]))
        d['target']=next(h for h in self.f.index['heads'] if h['number']!=self.target['number'])
        with self.assertRaisesRegex(ValueError,'自动识别结果'):self.prepare([d])
    def test_no_manual_target_needed_and_cancellation_leaves_no_mapping(self):
        d=self.f.document();stop=threading.Event();stop.set()
        with self.assertRaises(Cancelled):self.prepare([d],cancel=stop)
        with closing(pm.readonly(self.path)) as db:self.assertEqual(pm.mappings(db),{})
    def test_local_mapping_and_payment_rollback_together(self):
        plan=self.prepare([self.f.document()])
        with patch('payment_matching.apply',side_effect=RuntimeError('injected')):
            with self.assertRaisesRegex(RuntimeError,'injected'):lc.commit(self.path,plan)
        self.assertTrue(self.f.f.unpaid())
        with closing(pm.readonly(self.path)) as db:self.assertEqual(pm.mappings(db),{})
    def test_source_changed_after_preview_or_new_candidate_rejects_commit(self):
        d=self.f.document();plan=self.prepare([d]);self.f.duplicate_previous_day()
        with self.assertRaisesRegex(wr.Conflict,'生成范围'):lc.commit(self.path,plan)
        self.assertTrue(self.f.f.unpaid())

if __name__=='__main__':unittest.main()
