"""Cancelled, accepted and uncertain purchases keep their original identities."""
import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from test_aoxiang_workflows import head
from aoxiang_workflows import Workflows
from aoxiang_workflow_runner import Runner
from aoxiang_workflow_state import recoverable
from aoxiang_client import ComponentError


class WorkflowRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.s=Workflows(self.root)
    def tearDown(self):self.tmp.cleanup()
    def ready(self,count=2):
        j=self.s.prepare([head(i) for i in range(count)])
        for n in range(count):
            self.s.update(j['id'],position=n,changes={'state':'PREPARED','package':{'id':'original-'+str(n)}})
        return self.s.update(j['id'],state='READY')
    def upload(self,count=2):
        p=self.ready(count)
        return p,self.s.upload(p['id'],list(range(count)),p['sequence'])
    def test_cancel_mixed_batch_keeps_accepted_observable_and_skips_only_unstarted(self):
        _,j=self.upload()
        evidence={'uploadAttempted':True,'taskId':'original-task'}
        self.s.update(j['id'],position=0,changes={'state':'ACCEPTED','purchase_observation':evidence})
        stopped=self.s.cancel_queued(j['id'])
        self.assertEqual(stopped['state'],'PAUSED')
        self.assertEqual([i['state'] for i in stopped['items']],['ACCEPTED','SKIPPED'])
        self.assertEqual(stopped['items'][0]['purchase_observation'],evidence)
        reopened=Workflows(self.root);resumed=reopened.resume(j['id'])
        self.assertEqual(resumed['dispatch_keys'],[j['items'][0]['key']])
        self.assertEqual(reopened.next_queued()['id'],j['id'])
    def test_reconfirm_cancelled_preview_rearms_same_identity_once(self):
        p,j=self.upload();self.s.cancel_queued(j['id'])
        a=self.s.upload(p['id'],[0,1],p['sequence']);b=self.s.upload(p['id'],[0,1],p['sequence'])
        self.assertEqual(a['id'],j['id']);self.assertEqual(a,b)
        self.assertEqual(a['state'],'QUEUED');self.assertEqual(len(self.s.list()),2)
        self.assertEqual([i['package'] for i in a['items']],[i['package'] for i in j['items']])
    def test_old_cancelled_accepted_batch_can_recover_after_reopen(self):
        _,j=self.upload(1)
        self.s.update(j['id'],position=0,changes={'state':'ACCEPTED','purchase_observation':{'uploadAttempted':True,'taskId':'known'}})
        old=self.s.update(j['id'],state='CANCELLED')
        result=Workflows(self.root).recover_original(j['id'],old['sequence'])
        self.assertEqual(result['items'][0]['state'],'ACCEPTED');self.assertEqual(result['id'],j['id'])
    def test_unknown_attempt_is_not_cancelled_as_unstarted(self):
        for changes in ({'submission_started':True},{'purchase_observation':{'uploadAttempted':True}},
                        {'error':{'code':'RESPONSE_UNKNOWN'}},{'error':{'code':'SUBMISSION_UNKNOWN'}}):
            with self.subTest(changes=changes):
                p=self.s.prepare([head(str(len(self.s.list()))+'unknown')])
                self.s.update(p['id'],position=0,changes=changes)
                stopped=self.s.cancel_queued(p['id'])
                self.assertEqual(stopped['state'],'PAUSED');self.assertEqual(stopped['items'][0]['state'],'PENDING')
    def test_explicit_recovery_preserves_success_and_original_order(self):
        _,j=self.upload(3);success={'state':'BOUND','result':{'binding':{'orderId':'PO-original'}}}
        saved=self.s.update(j['id'],position=1,changes=success)['items'][1]
        stopped=self.s.cancel_queued(j['id'])
        restored=self.s.recover_original(j['id'],stopped['sequence'])
        self.assertEqual(restored['items'][1],saved)
        self.assertEqual(restored['dispatch_keys'],[j['items'][0]['key'],j['items'][2]['key']])
        self.assertEqual([i['item_id'] for i in restored['items']],[i['item_id'] for i in j['items']])
    def test_recovery_rejects_stale_and_running_updates(self):
        _,j=self.upload(1);stopped=self.s.cancel_queued(j['id'])
        with self.assertRaisesRegex(ValueError,'已变化'):self.s.recover_original(j['id'],j['sequence'])
        restored=self.s.recover_original(j['id'],stopped['sequence'])
        self.s.claim(j['id']);running=self.s.get(j['id'])
        self.assertEqual(self.s.recover_original(j['id'],running['sequence']),running)
        self.assertEqual(restored['items'][0]['package'],j['items'][0]['package'])
    def test_successful_batch_is_not_replayed(self):
        _,j=self.upload(1);self.s.update(j['id'],position=0,changes={'state':'BOUND'})
        j=self.s.update(j['id'],state='COMPLETED')
        self.assertFalse(recoverable(j));self.assertEqual(self.s.recover_original(j['id'],j['sequence']),j)
    def test_successor_reconfirmation_never_reactivates_predecessor(self):
        p,j=self.upload(1);self.s.cancel_queued(j['id']);j=self.s.get(j['id'])
        new=self.s.finish_and_new(j['id'],j['sequence'])
        self.assertEqual(self.s.upload(p['id'],[0],p['sequence'])['id'],new['id'])
        self.assertEqual(self.s.get(j['id'])['state'],'CANCELLED')
        self.assertFalse(new['items'][0].get('cancelled_unstarted'))
        self.s.update(new['id'],position=0,changes={'state':'BOUND'})
        completed=self.s.update(new['id'],state='COMPLETED')
        self.assertFalse(recoverable(completed))
    def test_finish_new_cannot_strand_accepted_item(self):
        _,j=self.upload();self.s.update(j['id'],position=0,changes={'state':'ACCEPTED'})
        j=self.s.cancel_queued(j['id'])
        with self.assertRaisesRegex(ValueError,'受理'):self.s.finish_and_new(j['id'],j['sequence'])
        self.assertEqual(self.s.get(j['id']),j)
    def test_single_accepted_recovery_does_not_revive_cancelled_sibling(self):
        _,j=self.upload();self.s.update(j['id'],position=0,changes={'state':'ACCEPTED','purchase_observation':{'taskId':'T','uploadAttempted':True}})
        stopped=self.s.cancel_queued(j['id']);other=copy.deepcopy(stopped['items'][1])
        r=self.s.recover_original(j['id'],stopped['sequence'],[stopped['items'][0]['key']])
        self.assertEqual(r['dispatch_keys'],[stopped['items'][0]['key']]);self.assertEqual(r['items'][1],other)
    def test_no_external_submit_on_unknown_original_result(self):
        _,j=self.upload(1);self.s.update(j['id'],position=0,changes={'submission_started':True})
        j=self.s.cancel_queued(j['id']);self.s.recover_original(j['id'],j['sequence'])
        calls=[]
        class Client:
            def request(self,action,**kw):
                calls.append(action)
                if action=='configuration':return {'purchase_pipeline':True}
                if action=='purchase.status':return {'state':'NEEDS_REVIEW','uploadAttempted':True,'taskId':None}
                raise AssertionError('must not dispatch '+action)
        with patch('aoxiang_upload_pipeline.require_orders'):
            r=Runner(self.s,Client(),self.root,self.root/'unused',cancel=threading.Event()).run(j['id'])
        self.assertEqual(r['items'][0]['state'],'REVIEW');self.assertNotIn('purchase.submit',calls)
        self.assertEqual(r['items'][0]['error']['code'],'SUBMISSION_UNKNOWN')
    def test_accepted_recovery_only_observes_original_task(self):
        _,j=self.upload(1);self.s.update(j['id'],position=0,changes={'state':'ACCEPTED','purchase_observation':{'uploadAttempted':True,'taskId':'T'}})
        j=self.s.cancel_queued(j['id']);self.s.resume(j['id']);calls=[]
        class Client:
            def request(self,action,**kw):
                calls.append((action,kw))
                if action=='configuration':return {'purchase_pipeline':True}
                if action=='purchase.start':return {}
                if action=='purchase.status':return {'state':'BOUND','active':False,'binding':{'orderId':'PO'},'uploadAttempted':True,'taskId':'T'}
                raise AssertionError(action)
        with patch('aoxiang_upload_pipeline.require_orders'):
            r=Runner(self.s,Client(),self.root,self.root/'unused',cancel=threading.Event()).run(j['id'])
        self.assertEqual(r['items'][0]['state'],'BOUND');self.assertNotIn('purchase.submit',[a for a,_ in calls])
        self.assertTrue(all(kw.get('purchase_id')=='original-0' for a,kw in calls if a.startswith('purchase.')))
