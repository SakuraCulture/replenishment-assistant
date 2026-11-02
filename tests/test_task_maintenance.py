import sys,tempfile,unittest,threading
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from test_aoxiang_workflows import head
from aoxiang_workflows import Workflows
from aoxiang_workflow_runner import Runner
from aoxiang_client import ComponentError

class TaskMaintenance(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.s=Workflows(self.t.name)
        self.job=self.s.prepare([head(1),head(2)])
    def failures(self):
        self.s.update(self.job['id'],position=0,changes={'state':'PREPARED','package':{'id':'immutable'}})
        return self.s.update(self.job['id'],state='COMPLETED_WITH_ERRORS',position=1,changes={'state':'FAILED','error':{'message':'network'}})
    def test_retry_keeps_identity_order_success_and_prevents_double_click(self):
        old=self.failures();new=self.s.retry_items(old['id'],[1],old['sequence'])
        self.assertEqual(new['id'],old['id']);self.assertEqual(new['items'][0],old['items'][0]);self.assertEqual(new['items'][1]['key'],old['items'][1]['key'])
        self.assertEqual(new['items'][1]['state'],'PENDING');self.assertEqual(len(self.s.list()),1)
        with self.assertRaises(ValueError):self.s.retry_items(old['id'],[1],old['sequence'])
    def test_retry_running_siblings_continue(self):
        old=self.failures();old=self.s.update(old['id'],state='RUNNING')
        new=self.s.retry_items(old['id'],[1],old['sequence']);self.assertEqual(new['state'],'RUNNING')
    def test_finish_new_only_failed_and_reuses_package(self):
        old=self.failures();new=self.s.finish_and_new(old['id'],old['sequence'])
        self.assertEqual([i['key'] for i in new['items']],[old['items'][1]['key']]);self.assertEqual(self.s.get(old['id'])['state'],'CANCELLED')
        again=self.s.finish_and_new(old['id'],self.s.get(old['id'])['sequence']);self.assertEqual(again['id'],new['id'])
    def test_unknown_cannot_transfer(self):
        old=self.s.update(self.job['id'],state='COMPLETED_WITH_ERRORS',position=0,changes={'state':'REVIEW'})
        with self.assertRaisesRegex(ValueError,'待核'):self.s.finish_and_new(old['id'],old['sequence'])
    def test_unknown_upload_queries_only_never_start(self):
        class Client:
            def __init__(self):self.calls=[]
            def request(self,action,**kw):
                self.calls.append(action);return {'state':'NEEDS_REVIEW','uploadAttempted':True,'receipt':None}
        client=Client();r=Runner(self.s,client,self.t.name,Path(self.t.name)/'business.db',cancel=threading.Event())
        with self.assertRaises(ComponentError):r.upload_item({'package':{'id':'p1'}},{},{})
        self.assertEqual(client.calls,['purchase.status'])

if __name__=='__main__':unittest.main()
