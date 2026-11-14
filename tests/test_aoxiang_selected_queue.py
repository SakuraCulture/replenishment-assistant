"""Isolated stale-order and selected-dispatch regressions, no platform calls."""
import copy
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from aoxiang_workflows import Workflows
from aoxiang_workflow_runner import Runner
from aoxiang_order_guard import deleted_orders
from runtime_support import Cancelled
from test_aoxiang_workflows import head


class SelectedQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.path=self.root/'workbench.sqlite3'
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE normal_orders(oid TEXT PRIMARY KEY,day TEXT,store TEXT)')
            db.executemany('INSERT INTO normal_orders VALUES(?,?,?)',[(str(n),head(n)['day'],str(n)) for n in (1,2,3)])
            db.execute('CREATE TABLE archive_visibility(oid TEXT,warehouse TEXT,deleted INTEGER)');db.commit()
        self.s=Workflows(self.root/'aoxiang',business_path=self.path);self.called=[];self.api=[];test=self
        class Client:
            def request(self,action,**kw):
                test.api.append(action)
                if action=='configuration':return {}
                raise AssertionError('禁止真实平台动作：'+action)
        self.r=Runner(self.s,Client(),self.root,self.path,cancel=threading.Event())

    def execute(self,job):
        def prepare(r,job,item,*args):
            self.called.append(item['key']);r.store.update(job['id'],position=item['position'],changes={'package':{'id':'p'+item['key']}})
            return 'PREPARED',{'purchase_id':'p'+item['key']}
        # This minimal-source fixture isolates selection/deletion dispatch and
        # already stubs all preparation. Payment eligibility is exercised with
        # real lifecycle sources in test_aoxiang_preflight_runner.
        with patch('aoxiang_workflow_runner.preflight_payment',return_value={}),patch.object(Runner,'prepare_item',prepare):
            return self.r.run(job['id'])

    def delete_order(self,oid):
        with closing(sqlite3.connect(self.path)) as db:db.execute('DELETE FROM normal_orders WHERE oid=?',(oid,));db.commit()

    def test_only_checked_execute_and_previews_can_upload_while_others_held(self):
        job=self.s.prepare([head(1),head(2),head(3)])
        job=self.s.execute_selected(job['id'],['BH-3','BH-1'],job['sequence']);out=self.execute(job)
        self.assertEqual(self.called,['BH-1','BH-3']);self.assertEqual([i['state'] for i in out['items']],['PREPARED','PENDING','PREPARED'])
        self.assertEqual(out['state'],'PAUSED')
        with self.assertRaisesRegex(ValueError,'已经完成'):self.s.resume(job['id'])
        upload=self.s.upload(out['id'],[0,2],out['sequence']);self.assertEqual([i['key'] for i in upload['items']],['BH-1','BH-3'])
        job=self.s.execute_selected(out['id'],['BH-2'],out['sequence']);self.execute(job)
        self.assertEqual(self.called,['BH-1','BH-3','BH-2'])

    def test_stop_resume_preserves_dispatch_range(self):
        j=self.s.prepare([head(1),head(2)]);j=self.s.execute_selected(j['id'],['BH-2'],j['sequence'])
        self.r.cancel.set()
        with self.assertRaises(Cancelled):self.r.run(j['id'])
        self.s.update(j['id'],state='PAUSED');self.s.resume(j['id']);self.r.cancel.clear();self.execute(j)
        self.assertEqual(self.called,['BH-2']);self.assertEqual(self.s.get(j['id'])['items'][0]['state'],'PENDING')

    def test_retry_selected_does_not_dispatch_unrelated_pending(self):
        j=self.s.prepare([head(1),head(2)]);j=self.s.update(j['id'],state='PAUSED',position=1,changes={'state':'FAILED'})
        j=self.s.retry_items(j['id'],[1],j['sequence']);self.execute(j);self.assertEqual(self.called,['BH-2'])

    def test_selection_compare_and_set_and_no_empty_or_completed_replay(self):
        j=self.s.prepare([head(1),head(2)])
        for keys in ([],['BH-4'],['BH-1','BH-1']):
            with self.assertRaises(ValueError):self.s.execute_selected(j['id'],keys,j['sequence'])
        with self.assertRaisesRegex(ValueError,'变化'):self.s.execute_selected(j['id'],['BH-1'],j['sequence']-1)
        out=self.execute(j)
        with self.assertRaisesRegex(ValueError,'就绪预览'):self.s.execute_selected(j['id'],['BH-1'],out['sequence'])

    def test_deleted_order_rejected_before_new_queue_and_runner_has_final_guard(self):
        j=self.s.prepare([head(1),head(2)]);self.delete_order('1')
        with self.assertRaisesRegex(ValueError,'已删除'):self.s.prepare([head(1)])
        out=self.execute(j)
        self.assertEqual(self.called,['BH-2']);self.assertEqual(len(out['items']),1)
        self.assertEqual(out['items'][0]['key'],'BH-2');self.assertEqual(out['state'],'READY')

    def test_reconcile_prunes_stale_local_only_and_preserves_platform_evidence(self):
        j=self.s.prepare([head(1),head(2),head(3)])
        self.s.update(j['id'],position=1,changes={'state':'REVIEW','package':{'id':'p2'},'purchase_observation':{'uploadAttempted':True,'taskId':'T2'}})
        self.delete_order('1');self.delete_order('2');self.s.reconcile_deleted()
        out=self.s.get(j['id']);self.assertEqual([i['key'] for i in out['items']],['BH-2','BH-3'])
        self.assertTrue(out['items'][0]['source_deleted']);self.assertEqual(out['items'][0]['package'],{'id':'p2'})
        self.assertEqual(out['items'][0]['purchase_observation']['taskId'],'T2')

    def test_soft_deleted_child_does_not_hide_sibling_warehouse(self):
        hb=head(1);sd={**hb,'warehouse':'山东仓','number':'BH-1-SD'}
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("INSERT INTO archive_visibility VALUES('1','河北仓',1)");db.commit()
        missing=deleted_orders(self.path,[{'key':'hb','head':hb},{'key':'sd','head':sd}])
        self.assertEqual(set(missing),{'hb'});self.s.prepare([sd])

    def test_delete_selected_local_only_cas_and_remaining_business_unchanged(self):
        j=self.s.prepare([head(1),head(2)]);before=self.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'变化'):self.s.delete_local_items(j['id'],['BH-1'],j['sequence']-1)
        out=self.s.delete_local_items(j['id'],['BH-1'],j['sequence'])
        self.assertEqual([i['key'] for i in out['items']],['BH-2']);self.assertEqual(self.path.read_bytes(),before)
        self.assertIsNone(self.s.delete_local_items(j['id'],['BH-2'],out['sequence']));self.assertEqual(self.s.list(),[])

    def test_cannot_delete_platform_identity_or_running_item(self):
        j=self.s.prepare([head(1)]);j=self.s.update(j['id'],position=0,changes={'package':{'id':'p1'}})
        with self.assertRaisesRegex(ValueError,'平台采购身份'):self.s.delete_local_items(j['id'],['BH-1'],j['sequence'])
        j=self.s.update(j['id'],state='RUNNING')
        with self.assertRaisesRegex(ValueError,'正在执行'):self.s.delete_local_items(j['id'],['BH-1'],j['sequence'])

    def test_all_deleted_local_batches_disappear_without_platform_request(self):
        self.s.prepare([head(1)]);self.delete_order('1');self.s.reconcile_deleted()
        self.assertEqual(self.s.list(),[]);self.assertEqual(self.api,[])

    def test_valid_queue_reconciliation_under_reader_lock_is_read_only(self):
        j=self.s.prepare([head(1)])
        with closing(sqlite3.connect(self.s.path)) as reader:
            reader.execute('BEGIN');reader.execute('SELECT * FROM ax_workflows').fetchall()
            self.s.reconcile_deleted()
            self.assertEqual(self.s.get(j['id']),j)

    def test_deleted_order_upload_not_attempted_is_blocked_but_bound_is_readable(self):
        item={'key':'BH-1','head':head(1),'package':{'id':'p1'}};self.delete_order('1')
        record={'state':'READY','uploadAttempted':False}
        with patch.object(self.r.client,'request',return_value=record) as calls:
            with self.assertRaisesRegex(ValueError,'已删除'):self.r.upload_item(item,{},[])
            self.assertEqual([c.args[0] for c in calls.call_args_list],['purchase.status'])
        record={'state':'BOUND','binding':{'orderId':'PO1'}}
        with patch.object(self.r.client,'request',return_value=record) as calls:
            self.assertEqual(self.r.upload_item(item,{},[]),('BOUND',{'binding':{'orderId':'PO1'}}))
            self.assertEqual(calls.call_count,1)


if __name__=='__main__':unittest.main()
