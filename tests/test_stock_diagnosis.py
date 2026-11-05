import sys,copy,unittest,threading
from pathlib import Path
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).parent)]
import test_automation_gateway as fixtures
import stock_diagnosis as service
import stock_diagnosis_adoption as adoption
from retry_profile_smoke import install

class DiagnosisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):fixtures.GatewayRegression.setUpClass()
    def setUp(self):
        self.f=fixtures.GatewayRegression();self.f.setUp();self.g=self.f.g;self.win=self.f.win;install(self.win,'B');self.win.concurrency.setValue(1)
    def tearDown(self):self.f.tearDown()
    def run_trial(self):return service.run(self.g.snapshot({}),{'stores':['S1'],'categories':['快销']},dict(self.win.results))
    def test_probability_only_complete_recalculation(self):
        original=self.g.token();session=self.run_trial();self.assertEqual(self.g.token(),original);self.assertEqual(len(session['options']),2)
        base=session['baseline'];self.assertEqual(base['results']['S1']['summary']['amount'],10)
        for item in session['options']:
            old=copy.deepcopy(base['prepared']['records']['S1']['preset']['config']);new=copy.deepcopy(item['prepared']['records']['S1']['preset']['config'])
            old.setdefault('profile_parameters',{}).setdefault('快销',{})['probability']=new['profile_parameters']['快销']['probability'];self.assertEqual(old,new)
            self.assertEqual(item['calculation']['results']['S1']['rows'][0]['AN'],base['results']['S1']['rows'][0]['AN'])
            self.assertIn('单件',item['comparison']['stores'][0]['distributions'])
            self.assertEqual(item['calculation']['results']['S1']['rows'][0]['_effective_probability'],item['changes'][0]['new'])
        service.strict_baseline(base['results'],base['results'])
    def test_existing_completed_baseline_is_strictly_compatible(self):
        from retry_profile_smoke import wait
        self.win.start_calculation();wait(self.win);before=self.g.token();s=self.run_trial();self.assertEqual(self.g.token(),before)
        service.strict_baseline({'S1':self.win.results['S1']},s['baseline']['results'])
    def test_invalid_steps_cancel_and_stale_baseline(self):
        session=self.run_trial();p=session['baseline']['prepared']
        for steps in ([.051],[0],[float('nan')],[.01,.02,.03]):
            with self.assertRaises(ValueError):service.candidates(p,['快销'],steps)
        cancel=threading.Event();cancel.set()
        with self.assertRaises(Exception):service.run(self.g.snapshot({}),{'stores':['S1'],'categories':['快销']},{},cancel=cancel)
        old=copy.deepcopy(session['baseline']['results']);old['S1']['rows'][0]['AS']=999
        with self.assertRaisesRegex(ValueError,'基线重算'):service.strict_baseline(old,session['baseline']['results'])
    def test_no_partial_store_comparison_and_fixed_predictor(self):
        session=self.run_trial();base=session['baseline']['results'];changed=copy.deepcopy(base);changed['S1']['rows'][0]['AN']+=1
        with self.assertRaisesRegex(ValueError,'固定业务字段'):service.metrics(base,changed)
        with patch.object(service.automation_jobs,'calculate',lambda *args:None):
            with self.assertRaisesRegex(ValueError,'未全部完成'):service.compute(session['baseline']['prepared'],lambda _:None,None)
    def test_adoption_undo_and_rollback_without_default_writes(self):
        s=self.run_trial();before=self.g.token();defaults=self.win.work_repository.stores();results=self.win.results
        adoption.adopt(self.g,s,0,'隔离验收',True)
        self.assertIn('stock_diagnosis_audit',self.win.results['S1']);self.assertEqual(self.win.work_repository.stores(),defaults)
        adoption.undo(self.g,s);self.assertEqual(self.g.token(),before);self.assertIs(self.win.results,results)
        s=self.run_trial()
        with patch.object(self.win,'touch',side_effect=RuntimeError('合成提交失败')):
            with self.assertRaisesRegex(RuntimeError,'合成提交失败'):adoption.adopt(self.g,s,0,'验收',True)
        self.assertEqual(self.g.token(),before);self.assertFalse(s['adopted'])
    def test_adoption_guards_and_saved_undo(self):
        s=self.run_trial();self.win.set_store_ui('S1',{'mode':2,'amount':'9','primary':'山东仓','selected':True})
        with self.assertRaisesRegex(ValueError,'变化'):adoption.adopt(self.g,s,0,'不能用旧结果',True)
        s=self.run_trial();adoption.adopt(self.g,s,0,'采用',True);self.win.saved_time='newly-saved'
        with self.assertRaisesRegex(ValueError,'保存或签发'):adoption.undo(self.g,s)
    def test_ai_api_paging_and_explicit_adoption(self):
        args={'operation_key':'trial1','context_token':self.g.token(),'stores':['S1'],'categories':['快销']}
        task=self.g.dispatch('start_stock_diagnosis',args);self.f.wait();self.assertEqual(self.g.journal.get(task['task_id'])['state'],'completed')
        r=self.g.dispatch('stock_diagnosis_results',{'task_id':task['task_id'],'candidate':1,'limit':1});self.assertEqual(r['page']['next_offset'],1)
        adopt={'task_id':task['task_id'],'candidate':1,'context_token':self.g.token(),'reason':'业务核对','accept_risks':True}
        result=self.g.dispatch('adopt_stock_diagnosis',adopt);self.assertTrue(result['adopted']);self.assertEqual(self.g.dispatch('adopt_stock_diagnosis',adopt),result)
        self.assertTrue(self.g.dispatch('undo_stock_diagnosis',{'task_id':task['task_id'],'context_token':result['context_token']})['undone'])
    def test_audit_survives_existing_job_and_sign_storage(self):
        from decimal import Decimal
        import normal_orders
        s=self.run_trial();s['options'][0]['comparison']['risks'].append({'store':'S1','sku':'A','coverage_after':Decimal('1.2500'),'risks':['合成软风险']})
        adoption.adopt(self.g,s,0,'审计保存验收',True);payload=self.win.snapshot()
        self.win.work_repository.save_job(self.win.job_day,payload)
        saved=self.win.work_repository.get_job(self.win.job_day)
        audit=saved['payload']['results']['S1']['stock_diagnosis_audit'];self.assertEqual(audit['risks'][-1]['coverage_after'],'1.2500')
        repo=normal_orders.Repository(self.win.work_repository)
        repo.confirm(payload,['S1'],saved['revision'],{'S1':None})
        signed=repo.get(self.win.job_day,'S1');self.assertEqual(signed['payload']['result']['stock_diagnosis_audit'],audit)

if __name__=='__main__':unittest.main()
