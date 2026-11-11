"""Durable workflow scheduling, explicit rebuild scope, no real platform/data."""
import copy
import sys
import tempfile
import threading
import sqlite3
import unittest
from pathlib import Path
from contextlib import ExitStack,closing
from unittest.mock import patch

sys.path[:0]=[str(Path(__file__).resolve().parents[1]/p) for p in ('src','tests')]
from test_aoxiang_workflows import head
from aoxiang_workflows import Workflows
from aoxiang_workflow_runner import Runner
from runtime_support import Cancelled


class Platform:
    def __init__(self,ids,*,pending=0,unknown=(),fail=(),cancel=None):
        self.records={i:{'id':i,'state':'RESERVED','uploadAttempted':False,'taskId':None,'error':None,'active':False} for i in ids}
        self.events=[];self.uploads=[];self.inflight=set();self.maximum=0;self.pending=pending;self.checks={};self.unknown=set(unknown);self.fail=set(fail);self.cancel=cancel
        for i in unknown:self.records[i].update(state='NEEDS_REVIEW',uploadAttempted=True)
    def request(self,action,**values):
        if action=='configuration':return {'configured':True,'enabled':True,'purchase_pipeline':True}
        i=values['purchase_id'];r=self.records[i]
        if action=='purchase.status':return copy.deepcopy(r)
        if action=='purchase.submit':
            if not r['uploadAttempted']:
                self.uploads.append(i);self.events.append(('upload',i));self.inflight.add(i);self.maximum=max(self.maximum,len(self.inflight))
                r.update(state='ACCEPTED',uploadAttempted=True,taskId='TASK-'+i)
            if self.cancel:self.cancel.set()
            return copy.deepcopy(r)
        if action=='purchase.start':
            self.events.append(('observe',i));self.checks[i]=self.checks.get(i,0)+1
            if i in self.fail:r.update(state='NEEDS_REVIEW',error={'code':'IMPORT_TASK_PARTIAL','message':'合成部分失败'});self.inflight.discard(i)
            elif self.checks[i]<=self.pending:r.update(state='ACCEPTED',error={'code':'TASK_PENDING','message':'合成处理中'})
            else:r.update(state='BOUND',error=None,binding={'orderId':'PO-'+i});self.inflight.discard(i)
            return copy.deepcopy(r)
        if action=='purchase.pause':return copy.deepcopy(r)
        raise AssertionError(action)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.now=1000
        self.store=Workflows(self.root,clock=lambda:self.now);self.stack=ExitStack()
        self.stack.enter_context(patch('aoxiang_upload_pipeline.require_orders'))
        self.stack.enter_context(patch('aoxiang_upload_pipeline.validate_for_upload'))
        self.stack.enter_context(patch('aoxiang_upload_pipeline.Directory'))
    def tearDown(self):self.stack.close();self.tmp.cleanup()
    def batch(self,count=3,limit=2):
        prep=self.store.prepare([head(i) for i in range(count)])
        for n in range(count):
            self.store.update(prep['id'],position=n,changes={'state':'PREPARED','package':{'id':'p'+str(n),'package_file':'unused','package_sha256':'a'*64,'plan_file':'unused','plan_sha256':'b'*64},'summary':{'amount':'1'}})
        ready=self.store.update(prep['id'],state='READY')
        return self.store.upload(prep['id'],list(range(count)),ready['sequence'],max_inflight=limit)
    def run_batch(self,job,client,*,cancel=None,wait=2):
        return Runner(self.store,client,self.root,self.root/'unused-isolated.sqlite3',cancel=cancel or threading.Event(),delay=.001,verification_interval=.001,verification_wait=wait).run(job['id'])
    def test_second_upload_precedes_first_observation_and_import_window_is_bounded(self):
        job=self.batch();client=Platform(['p0','p1','p2'],pending=1)
        out=self.run_batch(job,client)
        self.assertEqual(out['state'],'COMPLETED');self.assertEqual(client.uploads,['p0','p1','p2']);self.assertEqual(client.maximum,2)
        self.assertLess(client.events.index(('upload','p1')),client.events.index(('observe','p0')))
        self.assertEqual([i['result']['binding']['orderId'] for i in out['items']],['PO-p0','PO-p1','PO-p2'])
    def test_unknown_or_partial_item_does_not_block_other_orders(self):
        job=self.batch();client=Platform(['p0','p1','p2'],unknown=['p0'],fail=['p1'])
        out=self.run_batch(job,client)
        self.assertEqual(out['state'],'COMPLETED_WITH_ERRORS');self.assertEqual([i['state'] for i in out['items']],['REVIEW','REVIEW','BOUND'])
        self.assertEqual(client.uploads,['p1','p2']);self.assertEqual(out['items'][0]['error']['code'],'SUBMISSION_UNKNOWN')
    def test_pending_pass_yields_session_and_scheduler_can_choose_another_job(self):
        job=self.batch(2);client=Platform(['p0','p1'],pending=999)
        out=self.run_batch(job,client,wait=0)
        self.assertEqual(out['state'],'QUEUED');self.assertEqual([i['state'] for i in out['items']],['ACCEPTED','ACCEPTED'])
        self.assertIsNone(self.store.next_queued())
        other=self.store.prepare([head(9)]);self.assertEqual(self.store.next_queued()['id'],other['id'])
        client.pending=0;self.now+=1;out=self.run_batch(job,client)
        self.assertEqual(out['state'],'COMPLETED');self.assertEqual(client.uploads,['p0','p1'])
    def test_pause_and_restart_keep_exact_accepted_task_without_upload_replay(self):
        job=self.batch(2);cancel=threading.Event();client=Platform(['p0','p1'],cancel=cancel)
        with self.assertRaises(Cancelled):self.run_batch(job,client,cancel=cancel)
        self.assertEqual(self.store.get(job['id'])['items'][0]['state'],'ACCEPTED')
        self.store.recover();cancel.clear();client.cancel=None
        out=self.run_batch(job,client,cancel=cancel)
        self.assertEqual(out['state'],'COMPLETED');self.assertEqual(client.uploads,['p0','p1'])
    def test_limit_one_keeps_serial_submission_and_invalid_limits_reject(self):
        job=self.batch(limit=1);client=Platform(['p0','p1','p2']);self.run_batch(job,client)
        self.assertEqual(client.maximum,1)
        p=self.store.prepare([head(9)]);p=self.store.update(p['id'],state='READY')
        with self.assertRaisesRegex(ValueError,'上限'):self.store.upload(p['id'],[0],p['sequence'],max_inflight=0)
    def test_legacy_interrupted_running_item_reads_original_receipt_instead_of_disappearing(self):
        job=self.batch(1);self.store.update(job['id'],position=0,changes={'state':'RUNNING'})
        self.store.recover();client=Platform(['p0'])
        client.records['p0'].update(state='ACCEPTED',uploadAttempted=True,taskId='TASK-p0')
        out=self.run_batch(job,client)
        self.assertEqual(out['items'][0]['state'],'BOUND');self.assertEqual(client.uploads,[])
    def test_selected_dispatch_does_not_submit_or_change_unselected_orders(self):
        job=self.batch();before=self.store.get(job['id'])['items'][2]
        job=self.store.execute_selected(job['id'],[job['items'][0]['key'],job['items'][1]['key']],job['sequence'])
        client=Platform(['p0','p1','p2']);out=self.run_batch(job,client)
        self.assertEqual(out['state'],'PAUSED');self.assertEqual(client.uploads,['p0','p1']);self.assertEqual(out['items'][2],before)
    def test_rebuild_unknown_and_bound_items_preserves_unselected_package_and_new_operation(self):
        job=self.batch();self.store.update(job['id'],position=0,changes={'state':'REVIEW','error':{'code':'SUBMISSION_UNKNOWN'}})
        self.store.update(job['id'],position=1,changes={'state':'BOUND','result':{'binding':{'orderId':'PO1'}}})
        old=self.store.update(job['id'],state='COMPLETED_WITH_ERRORS');untouched=copy.deepcopy(old['items'][2])
        new=self.store.rebuild_selected(old['id'],[old['items'][0]['key'],old['items'][1]['key']],old['sequence'],[head(i) for i in range(3)])
        after=self.store.get(old['id']);self.assertEqual(after['items'][2],untouched)
        self.assertEqual([i['state'] for i in after['items'][:2]],['SKIPPED','SKIPPED'])
        self.assertEqual([i['rebuild']['purchase_id'] for i in new['items']],['p0','p1'])
        self.assertNotIn('package',new['items'][0]);self.assertEqual(len({i['rebuild']['operation_id'] for i in new['items']}),2)
        again=self.store.rebuild_selected(old['id'],[i['key'] for i in old['items'][:2]],old['sequence'],[head(i) for i in range(3)])
        self.assertEqual(again['id'],new['id'])
    def test_rebuild_does_not_retarget_same_number_to_new_oid_or_running_task(self):
        job=self.batch(1);old=self.store.update(job['id'],state='COMPLETED_WITH_ERRORS');wrong={**head(0),'oid':'different'}
        with self.assertRaisesRegex(ValueError,'身份不符'):self.store.rebuild_selected(old['id'],[old['items'][0]['key']],old['sequence'],[wrong])
        old=self.store.update(job['id'],state='RUNNING')
        with self.assertRaisesRegex(ValueError,'安全退出'):self.store.rebuild_selected(old['id'],[old['items'][0]['key']],old['sequence'],[head(0)])
    def test_rebuild_stops_selected_purchase_schedule_and_preserves_other_schedule(self):
        job=self.batch(2);records=[{'id':'p'+str(i),'basis':head(i),'binding':{'orderId':'PO'+str(i)}} for i in range(2)]
        self.store.configure_schedules(records,5,True)
        receipt=self.store.receipt(records)
        old=self.store.update(job['id'],state='COMPLETED_WITH_ERRORS')
        before=self.store.schedule('p1');self.store.rebuild_selected(old['id'],[old['items'][0]['key']],old['sequence'],[head(0),head(1)])
        self.assertFalse(self.store.schedule('p0')['enabled']);self.assertEqual(self.store.schedule('p1'),before)
        receipt=self.store.get(receipt['id']);self.assertEqual([i['state'] for i in receipt['items']],['SKIPPED','PENDING'])
    def test_related_pending_prepare_is_ended_without_touching_unselected_item(self):
        job=self.batch(1);other=self.store.prepare([head(0),head(9)]);untouched=copy.deepcopy(other['items'][1])
        old=self.store.update(job['id'],state='COMPLETED_WITH_ERRORS')
        new=self.store.rebuild_selected(old['id'],[old['items'][0]['key']],old['sequence'],[head(0)])
        related=self.store.get(other['id']);self.assertEqual(related['items'][1],untouched)
        self.assertEqual(related['items'][0]['rebuild_to'],new['id']);self.assertEqual(related['items'][0]['state'],'SKIPPED')
    def test_mixed_existing_successor_and_new_item_is_rejected_atomically(self):
        job=self.batch(2);old=self.store.update(job['id'],state='COMPLETED_WITH_ERRORS')
        self.store.rebuild_selected(old['id'],[old['items'][0]['key']],old['sequence'],[head(0),head(1)])
        before=self.store.get(old['id'])
        with self.assertRaisesRegex(ValueError,'后继'):self.store.rebuild_selected(old['id'],[i['key'] for i in old['items']],before['sequence'],[head(0),head(1)])
        self.assertEqual(self.store.get(old['id']),before)


class CurrentHeadTests(unittest.TestCase):
    def test_read_current_revision_without_retargeting_deleted_or_recycled_original(self):
        from aoxiang_rebuild import current_heads
        from warehouse_orders import child_number
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'isolated.sqlite3'
            with closing(sqlite3.connect(path)) as db,db:
                db.executescript('CREATE TABLE normal_orders(oid TEXT,day TEXT,store TEXT,number TEXT,current INTEGER);CREATE TABLE normal_revisions(oid TEXT,revision INTEGER,digest TEXT);CREATE TABLE archive_visibility(oid TEXT,warehouse TEXT,deleted INTEGER,token INTEGER);')
                db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?)',('original','2026-11-10','S','BH-20261110-S',2))
                db.execute('INSERT INTO normal_revisions VALUES(?,?,?)',('original',2,'current-digest'))
                db.execute('INSERT INTO archive_visibility VALUES(?,?,?,?)',('original','河北仓',0,7))
            h={**head(0),'oid':'original','store':'S','number':child_number('BH-20261110-S','河北仓'),'warehouse':'河北仓'}
            actual=current_heads(path,[{'head':h}])[0]
            self.assertEqual(actual['revision'],2);self.assertEqual(actual['digest'],'current-digest');self.assertEqual(actual['visibility_token'],7)
            with closing(sqlite3.connect(path)) as db,db:db.execute('UPDATE archive_visibility SET deleted=1')
            with self.assertRaisesRegex(ValueError,'回收站'):current_heads(path,[{'head':h}])
            with closing(sqlite3.connect(path)) as db,db:db.execute("UPDATE normal_orders SET oid='new-same-number'")
            with self.assertRaisesRegex(ValueError,'已删除'):current_heads(path,[{'head':h}])


if __name__=='__main__':unittest.main()
