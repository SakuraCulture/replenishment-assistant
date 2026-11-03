"""Portable policy boundaries; no real Excel or user repositories."""
import sys,copy,unittest,tempfile,threading
from pathlib import Path
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).parent)]
import model,input_adapter,restock_policy as policy,parameter_schema as schema
import new_products,preissue,restock_trial,store_parameter_import as spi,work_repository as wr
import result_semantics as sem
from test_rules import row,META
from test_preissue import result,edit,commit,listing,row as edit_row

class ModelSwitchTests(unittest.TestCase):
    def test_extreme_boundary_and_cartons(self):
        for n in (29,30,31,45):
            for step in (1,6,24):
                for k,l in ((0,0),(1,0),(0,1),(0,.5)):
                    data=row(H=0,I=0,J=0,K=k,L=l,N=n,O=step)
                    old=model.calculate(data);off=model.calculate(data,extreme_restore=False)
                    if n<30:self.assertEqual(old,off)
                    else:
                        self.assertEqual(off['AS'],0);self.assertEqual(off['BQ'],0);self.assertEqual(off['BV'],0)
                        self.assertEqual(off['_extreme_restore']['suppressed_quantity'],old['BO'])
                        self.assertIn('已关闭',off['BM'])
                    off['_meta']={'export_row':2}
                    self.assertEqual(input_adapter.business_checks([off],model.Rules(),extreme_restore=False)['failures'],[])
    def test_on_default_and_reenable_exact(self):
        data=row(H=0,I=0,J=0,K=0,L=0,N=45,O=24)
        a=model.calculate(data);self.assertEqual(a,model.calculate(data,extreme_restore=True))
        b=model.calculate(model.calculate(data,extreme_restore=False))
        self.assertEqual(a,b)
    def test_only_extreme_no_sales_affected(self):
        for n in (None,'',0,1,29,30,31,45):
            for sales in ((1,7,30),(.07,1,1)):
                d=row(H=sales[0],I=sales[1],J=sales[2],K=0,L=0,N=n)
                a=model.calculate(d);b=model.calculate(d,extreme_restore=False)
                self.assertEqual(a,b);self.assertGreater(b['AS'],0)
    def test_missing_data_and_unknown_step_not_zero(self):
        for fields in ({'J':None},{'O':None},{'K':None},{'N':'bad'}):
            d=row(H=0,I=0,J=0,K=0,L=0,N=45);d.update(fields)
            self.assertIsInstance(model.calculate(d,extreme_restore=False)['AS'],str)
    def test_no_fake_disable_or_relaxed_minimum(self):
        d=model.calculate(row(H=0,I=0,J=0,K=0,L=0,N=45),extreme_restore=False);d['_meta']={'export_row':2}
        self.assertTrue(input_adapter.business_checks([d],model.Rules())['failures'])
        d['AS']=24;self.assertTrue(input_adapter.business_checks([d],model.Rules(),extreme_restore=False)['failures'])
        with self.assertRaises(ValueError):model.calculate(row(),extreme_restore=0)

class ConfigurationTests(unittest.TestCase):
    def test_legacy_nonmutation_strict_values_and_roundtrip(self):
        c=schema.standard();before=copy.deepcopy(c);signature=schema.signature(c)
        self.assertEqual(policy.values(c),dict(extreme_restore=True,new_stocking=True))
        self.assertEqual(policy.patch(c,dict(new_stocking=True)),c);self.assertEqual(schema.signature(c),signature)
        for e in (False,True):
            for n in (False,True):schema.validate(policy.patch(c,dict(extreme_restore=e,new_stocking=n)))
        for v in (0,'false',None):
            with self.assertRaises(ValueError):schema.validate({**c,policy.KEY:{'new_stocking':v}})
        self.assertEqual(c,before)
    def test_excel_patch_only_supplied_fields_and_blank_roundtrip(self):
        original=spi.base();original['preset']['config']['parameters']['probability']=.95
        rowdata={'values':{('新品铺设',):'关闭'}};v=spi.patch(rowdata,original)
        self.assertEqual(v['preset']['config']['parameters'],original['preset']['config']['parameters'])
        self.assertFalse(policy.values(v['preset']['config'])['new_stocking'])
        delta=list(spi.changes(original,v));self.assertEqual(len(delta),1);self.assertEqual(delta[0][1:],('开启','关闭'))
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/'params.xlsx';spi.write(path,{'001':v});data=spi.read([path]);again=spi.patch(data['rows'][0],original)
            self.assertEqual(again['preset']['config'],v['preset']['config'])
    def test_repository_legacy_new_preset_and_conflict(self):
        with tempfile.TemporaryDirectory() as t:
            repo=wr.Repository(t);a=spi.base();repo.save_stores({'001':a});a=repo.stores()['001']
            b=spi.patch({'values':{('极端缺货自动恢复',):'关闭'}},a)
            repo.save_stores({'001':b},expected={'001':a})
            with self.assertRaises(wr.Conflict):repo.save_stores({'001':a},expected={'001':a})
            self.assertEqual(repo.stores()['001'],b)
            p=repo.save_preset('关闭恢复',b['preset']['config']);self.assertEqual(repo.presets()[1]['config'],p['config'])

def sample(budget=None):
    a=edit_row('a',6,1,2);a.update(H=1,I=7,J=30)
    b=edit_row('new',0,6,2);b.update(H=0,I=0,J=0,K=0,L=0,N=None)
    r=result([a,b],budget);r['config']=schema.standard();return r

def preview_new_change(current,base,enabled,drafts=None):
    cfg=policy.patch(current['config'],{'new_stocking':enabled})
    return restock_trial.prepare(None,{'S1':{'parameter_config':current['config']}},{'S1':current},{'S1':base},drafts or {},{'S1':cfg},None,'2026-11-03')

class NewSwitchTests(unittest.TestCase):
    def test_guards_selection_manual_and_list_but_not_ordinary(self):
        r=sample();r['config']=policy.patch(r['config'],{'new_stocking':False})
        with self.assertRaisesRegex(ValueError,'已关闭'):new_products.apply(r,{'new':6})
        with self.assertRaisesRegex(ValueError,'已关闭'):preissue.preview(r,{'new':edit(6)},'2026-11-03')
        p=preissue.preview(r,{'a':edit(7)},'2026-11-03');self.assertTrue(p['can_apply'])
        ls=listing(['new']);p=preissue.list_edits(r,ls,'minimum');self.assertEqual(p['edits'],{});self.assertIn('已关闭',p['pending'][0]['reason'])
        ls=listing(['a']);p=preissue.list_edits(r,ls,'minimum');self.assertIn('已关闭',p['pending'][0]['reason'])
    def test_new_only_no_prediction_recompute_and_rebudget(self):
        for budget in (None,0,12,20):
            b=sample(budget);c=new_products.apply(b,{'new':6});before=copy.deepcopy(c)
            with patch('batch_service.calculate_store',side_effect=AssertionError('Must not recalculate model')):
                p=preview_new_change(c,b,False)
            out=restock_trial.finalize(p,{'S1':c})['S1']
            self.assertEqual([sem.order_quantity(d) for d in out['rows']],[sem.order_quantity(d) for d in b['rows']])
            self.assertFalse(policy.result_values(out)['new_stocking']);self.assertEqual(c,before)
    def test_manual_override_cannot_hide_new_list_origin(self):
        r=sample();ls=listing(['a']);e=edit(7,list_revision=ls['revision'])
        v=preissue.preview(r,{'a':e},'2026-11-03',lists={ls['revision']:ls});c=preissue.finalize(v,[x['id'] for x in v['risks']])
        trial=preview_new_change(c,r,False)
        self.assertEqual(trial['stores']['S1']['removed_edits'],['a'])
        r['config']=policy.patch(r['config'],{'new_stocking':False})
        with self.assertRaisesRegex(ValueError,'已关闭'):preissue.preview(r,{'a':e},'2026-11-03',lists={ls['revision']:ls})
    def test_manual_new_removed_other_edits_preserved_and_risks_reconfirmed(self):
        b=sample();p=preissue.preview(b,{'a':edit(7),'new':edit(6)},'2026-11-03')
        current=preissue.finalize(p,[x['id'] for x in p['risks']]);trial=preview_new_change(current,b,False)
        self.assertEqual(trial['stores']['S1']['removed_edits'],['new'])
        out=restock_trial.finalize(trial,{'S1':current},[r['id'] for r in trial['risks']])['S1']
        self.assertEqual([sem.order_quantity(d) for d in out['rows']],[7,0]);preissue.validate_result(out,'2026-11-03')
    def test_stale_preview_and_cancel_no_mutation(self):
        b=sample();c=new_products.apply(b,{'new':6});trial=preview_new_change(c,b,False)
        stale=copy.deepcopy(c);stale['rows'][0]['AS']=999
        with self.assertRaises(ValueError):restock_trial.finalize(trial,{'S1':stale})
        event=threading.Event();event.set()
        from runtime_support import Cancelled
        with self.assertRaises(Cancelled):restock_trial.prepare(None,{}, {},{}, {},{'S1':{}},None,'2026-11-03',cancel=event)
    def test_reenable_does_not_auto_select(self):
        b=sample();b['config']=policy.patch(b['config'],{'new_stocking':False})
        out=restock_trial.finalize(preview_new_change(b,b,True),{'S1':b})['S1']
        self.assertNotIn('new_products',out);self.assertEqual(sem.order_quantity(out['rows'][1]),0)

if __name__=='__main__':unittest.main(verbosity=2)
