import copy,os,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from workbench import Workbench
from automation_gateway import Gateway
from automation_protocol import CommandError
from retry_profile_smoke import install,quantities,profile
import automation_jobs,batch_service

class GatewayRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,REPLENISHMENT_DATA_DIR=self.tmp.name);self.env.start()
        self.win=Workbench();self.g=Gateway(self.win);self.g.enable()
    def wait(self):
        end=time.monotonic()+45
        while self.g.worker and time.monotonic()<end:QTest.qWait(10)
        self.assertIsNone(self.g.worker)
    def tearDown(self):
        import shiboken6
        if self.g.worker:self.g.worker.cancel.set();self.wait()
        self.g.disable();self.win._discard_close=True;self.win.job_dirty=False;self.win.close();self.app.processEvents();shiboken6.delete(self.win);self.env.stop();self.tmp.cleanup()
    def prepare(self,key,**kw):
        p=self.g.prepare_calculation({'context_token':self.g.token(),'operation_key':key,'mode':'trial',**kw});self.wait()
        self.assertEqual(self.g.journal.get(p['task_id'])['state'],'prepared');return p['task_id']
    def test_A_restore_B_then_AI_failure_retry_uses_B(self):
        install(self.win,'B');snapshot=copy.deepcopy(self.win.snapshot());self.win.work_repository.save_job(self.win.job_day,snapshot)
        saved=self.win.work_repository.get_job(self.win.job_day)
        install(self.win,'A');self.win._calculation_profile=profile('A')
        self.win.restore_job(saved);pid=self.prepare('B',stores=['S1'])
        def fail(p,codes,notify,cancel):
            for s in codes:notify(s,'计算失败',None,'合成瞬时失败')
        with patch.object(automation_jobs,'calculate',fail):t=self.g.run_calculation({'preview_id':pid});self.wait()
        retry=self.g.retry_failed({'task_id':t['task_id'],'stores':['S1'],'operation_key':'B-retry'});self.wait()
        r=self.g.require_result(t['task_id'])['results']['S1']
        self.assertEqual(quantities(r),{'A':0.,'B':10.});self.assertEqual(r['summary']['amount'],10)
        self.assertEqual(self.g.journal.get(t['task_id'])['state'],'completed')
    def test_active_plan_is_not_reapplied_over_manual_current_parameters(self):
        install(self.win,'B');self.win.active_plan={'id':'old-plan','name':'old'}
        self.win.overrides['S1']=copy.deepcopy(self.win.config_for('S1')['warehouses'])
        self.win.overrides['S1']['山东仓']=(11,2)
        snapshot=self.g.snapshot({});self.assertIsNone(snapshot['plan'])
        p=automation_jobs.prepare(snapshot,{'mode':'trial','stores':['S1']})
        self.assertEqual(p['jobs']['S1']['warehouses']['山东仓'],(11,2))
    def test_reconnect_and_validation_do_not_cross_workspace(self):
        install(self.win,'B');pid=self.prepare('x')
        with self.assertRaises(CommandError):self.g.prepare_calculation({'context_token':self.g.token(),'operation_key':'x','mode':'current'})
        self.g.disable();self.g.enable()
        self.assertEqual(self.g.journal.get(pid)['state'],'prepared')
        self.assertEqual(self.g.workbench_context({})['selected_stores'],['S1','S2'])

    def test_result_cache_is_bounded_and_adopted_results_survive(self):
        install(self.win,'B'); ids=[]
        def calculate(p,codes,notify,cancel):
            for s in codes:notify(s,'已完成',{'rows':[],'summary':{}},'')
        with patch.object(automation_jobs,'calculate',calculate):
            for n in range(4):
                pid=self.prepare('cache-'+str(n),stores=['S1'])
                t=self.g.run_calculation({'preview_id':pid});self.wait();ids.append(t['task_id'])
                if n==0:self.win.results['retained']=self.g.require_result(t['task_id'])['results']['S1']
        with self.assertRaises(CommandError):self.g.require_result(ids[0])
        self.assertTrue(self.g.journal.get(ids[0])['result_evicted'])
        self.assertEqual(len([r for r in self.g.calculations.values() if isinstance(r,dict)]),3)
        self.assertEqual(self.win.results['retained'],{'rows':[],'summary':{}})

if __name__=='__main__':unittest.main()
