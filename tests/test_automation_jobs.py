"""Frozen AI preparation: source semantics, plan scopes and priority isolation."""
import copy
import sys
import tempfile
import threading
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import automation_jobs as jobs
import parameter_maintenance as pm
import store_parameter_import as spi
import work_repository as wr
import batch_service
from retry_profile_smoke import loaded, profile, quantities
from runtime_support import Cancelled


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.repo = wr.Repository(self.tmp.name)
        records = {s: spi.base() for s in ('S1','S2')}
        for r in records.values(): r['ui'].update(mode=2, amount='10')
        self.s = dict(loaded=loaded(), input_dirty=False, selected=['S2','S1'], current_records=records,
            defaults={}, budgets={}, routes={}, mapping=pm.mapping_snapshot(self.repo.path), catalogs={},
            legacy=True, products='', profile=profile('B'), plan=None, workers=1, context_token='x',
            dbpath=str(self.repo.path), day=wr.today(), origins={}, previous_defaults={})
        self.a = dict(mode='trial', context_token='x', operation_key='test')
    def tearDown(self): self.tmp.cleanup()

    def test_preparation_preserves_priority_B_and_does_not_mutate_source(self):
        original=copy.deepcopy(self.s); p=jobs.prepare(self.s,self.a)
        self.assertEqual(p['codes'],['S2','S1']); self.assertEqual(self.s,original)
        for s in p['codes']:
            r=batch_service.calculate_store(p['loaded'],s,p['jobs'][s],p['profile'])
            self.assertEqual(quantities(r),{'A':0.,'B':10.})
        self.assertEqual(self.repo.stores(),{})

    def test_plan_only_patches_selected_parameters(self):
        from replenishment_plans import Repository
        c=copy.deepcopy(self.s['current_records']['S1']['preset']['config'])
        c['warehouses']['山东仓']=[9,4]
        repo=Repository(self.repo.path)
        # Plan payload matches the actual durable format; use repository save.
        plan=repo.save({'name':'测试预案','stores':[{'code':'S1','name':'门店1'}],
              'config':c,'parameter_fields':[['warehouses','山东仓',0]]})
        self.s['plan']=plan
        p=jobs.prepare(self.s,self.a)
        self.assertEqual(p['codes'],['S1'])
        self.assertEqual(p['records']['S1']['preset']['config']['warehouses']['山东仓'][0],9)
        self.assertEqual(p['records']['S1']['preset']['config']['warehouses']['山东仓'][1],self.s['current_records']['S1']['preset']['config']['warehouses']['山东仓'][1])
        self.assertEqual(p['jobs']['S1']['budget'],10)

    def test_missing_store_unchecked_input_and_unknown_parameter_are_not_guessed(self):
        with self.assertRaisesRegex(ValueError,'缺少'): jobs.prepare(self.s,{**self.a,'stores':['missing']})
        self.s['input_dirty']=True
        with self.assertRaisesRegex(ValueError,'尚未检查'): jobs.prepare(self.s,self.a)
        self.s['input_dirty']=False
        p=jobs.prepare(self.s,{**self.a,'parameter_changes':[{'store':'S1','path':['budget','amount'],'value':0}]})
        self.assertFalse(p['report']['ready']);self.assertNotIn('S1',p['jobs'])

    def test_conflict_remains_blocked_and_cancel_is_not_success(self):
        self.s['loaded']['blocked']={'S1':True}
        self.assertFalse(jobs.prepare(self.s,self.a)['report']['ready'])
        cancel=threading.Event();cancel.set()
        with self.assertRaises(Cancelled):jobs.prepare(self.s,self.a,cancel=cancel)

    def test_inherited_category_parameters_show_effective_value(self):
        r=spi.base();r['preset']['config']['parameters']['probability']=.95
        r['preset']['config']['profile_parameters']={}
        rows=jobs.parameters(r)
        found=next(x for x in rows if x['path']==['profile_parameters','快销','probability'])
        self.assertEqual(found['value'],.95);self.assertEqual(found['source'],'门店通用参数')


if __name__=='__main__':unittest.main()
