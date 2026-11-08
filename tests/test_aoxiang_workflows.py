import copy
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src')]
from aoxiang_workflows import Workflows
from aoxiang_session import SessionGate
from aoxiang_workflow_runner import Runner
from aoxiang_client import ComponentError
from runtime_support import Cancelled

def head(n):
    return {'oid':str(n),'day':'2026-11-02','store':str(n),'name':'门店'+str(n),'warehouse':'河北仓','number':'BH-'+str(n),'revision':1,'current_revision':1,'digest':'a'*64,'visibility_token':'visible'}

def record(n):
    return {'id':'p'+str(n),'basis':head(n),'binding':{'orderId':'PO'+str(n)}}


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.now=1000
        self.s=Workflows(self.root,clock=lambda:self.now)
    def tearDown(self):self.tmp.cleanup()

    def test_order_duplicates_preview_gate_stale_confirmation_and_idempotent_upload(self):
        p=self.s.prepare([head(2),head(1)])
        self.assertEqual([i['head']['store'] for i in p['items']],['2','1'])
        with self.assertRaisesRegex(ValueError,'已有未完成'):self.s.prepare([head(1)])
        with self.assertRaisesRegex(ValueError,'尚未停止'):self.s.upload(p['id'],[0],p['sequence'])
        for n in range(2):self.s.update(p['id'],position=n,changes={'state':'PREPARED','package':{'id':'p'+str(n)}})
        ready=self.s.update(p['id'],state='READY')
        with self.assertRaisesRegex(ValueError,'预览已变化'):self.s.upload(p['id'],[0],p['sequence'])
        upload=self.s.upload(p['id'],[0,1],ready['sequence'])
        self.assertEqual(self.s.upload(p['id'],[0,1],ready['sequence'])['id'],upload['id'])
        with self.assertRaisesRegex(ValueError,'部分预览已确认'):self.s.upload(p['id'],[1],ready['sequence'])
        self.assertEqual([i['head']['store'] for i in upload['items']],['2','1'])

    def test_periodic_opt_in_order_duplicate_exclusion_backoff_and_manual_pause(self):
        self.assertIsNone(self.s.enqueue_due())
        records=[record(2),record(1)]
        self.s.configure_schedules(records,5,False);self.assertIsNone(self.s.enqueue_due())
        self.s.configure_schedules(records,5,True)
        manual=self.s.receipt([records[0]])
        due=self.s.enqueue_due();self.assertEqual([i['purchase_id'] for i in due['items']],['p1'])
        self.assertIsNone(self.s.enqueue_due())
        self.s.observe_schedule('p1',{'state':'NEEDS_REVIEW','terminal':False},error={'code':'TASK_PENDING'})
        self.s.update(due['id'],state='COMPLETED_WITH_ERRORS');self.now+=599
        self.assertIsNone(self.s.enqueue_due());self.now+=1
        again=self.s.enqueue_due();self.assertEqual(again['items'][0]['purchase_id'],'p1')
        self.s.configure_schedules([records[1]],5,False);self.s.update(again['id'],state='COMPLETED');self.now+=10000
        self.assertIsNone(self.s.enqueue_due())

    def test_terminal_latch_stops_routine_but_retries_final_task_and_keeps_after_restart(self):
        r=record(1);self.s.configure_schedules([r],5,True);job=self.s.enqueue_due()
        self.s.observe_schedule('p1',{'state':'PAUSED','terminal':True})
        s=self.s.schedules()[0];self.assertFalse(s['enabled']);self.assertTrue(s['final_pending'])
        self.s.update(job['id'],state='COMPLETED');self.now+=300
        final=self.s.enqueue_due();self.assertIsNotNone(final)
        self.s.update(final['id'],state='RUNNING');self.s.recover()
        self.assertEqual(self.s.get(final['id'])['state'],'YIELDED')
        self.s.observe_schedule('p1',{'state':'SYNCED','terminal':True});self.s.update(final['id'],state='COMPLETED');self.now+=10000
        self.assertIsNone(self.s.enqueue_due());self.assertTrue(self.s.schedules()[0]['terminal'])
        with self.assertRaisesRegex(ValueError,'不能重新启用'):self.s.configure_schedules([r],5,True)

    def test_review_stops_automatic_retry_and_multi_record_config_rolls_back(self):
        self.s.configure_schedules([record(1)],5,True);self.s.observe_schedule('p1',{'state':'NEEDS_REVIEW','terminal':True},review=True)
        self.now+=10000;self.assertIsNone(self.s.enqueue_due())
        with self.assertRaises(ValueError):self.s.configure_schedules([record(2),record(1)],10,True)
        self.assertEqual([s['id'] for s in self.s.schedules()],['p1'])

    def test_cancel_never_erases_packages_and_corrupt_tables_not_recreated(self):
        j=self.s.prepare([head(1)]);self.s.update(j['id'],position=0,changes={'package':{'id':'original'}})
        self.s.cancel_queued(j['id']);self.assertEqual(self.s.get(j['id'])['items'][0]['package']['id'],'original')
        with self.s.db(True) as db:db.execute('DROP TABLE ax_schedules')
        with self.assertRaisesRegex(ValueError,'结构缺失'):self.s.list()

    def test_foreground_preempts_then_waits_for_real_release_and_blocks_new_background(self):
        gate=SessionGate();token=gate.claim_background();ticket=gate.request_foreground();self.assertTrue(token.is_set());self.assertIsNone(gate.claim_background())
        passed=threading.Event();thread=threading.Thread(target=lambda:(ticket.wait(),passed.set()));thread.start()
        self.assertFalse(passed.wait(.05));gate.release_background(token);self.assertTrue(passed.wait(1));thread.join()
        self.assertIsNone(gate.claim_background());ticket.release();new=gate.claim_background();self.assertIsNotNone(new);gate.release_background(new)

    def test_foreground_cancel_does_not_release_ongoing_background(self):
        gate=SessionGate();token=gate.claim_background();ticket=gate.request_foreground()
        with self.assertRaisesRegex(RuntimeError,'取消等待'):ticket.wait(lambda:True)
        ticket.release();self.assertIsNone(gate.claim_background());gate.release_background(token)

    def test_history_window_keeps_all_active_and_oldest_queued_not_last_completion(self):
        first=self.s.prepare([head(1)]);self.s.update(first['id'],state='PAUSED')
        for n in range(2,8):
            j=self.s.prepare([head(n)]);self.s.update(j['id'],state='COMPLETED')
        newest=self.s.prepare([head(8)])
        self.assertEqual([j['id'] for j in self.s.list(limit=1)],[first['id'],newest['id']])
        self.s.resume(first['id']);self.assertEqual(self.s.next_queued()['id'],first['id'])

    def test_many_busy_schedules_do_not_starve_later_due_orders(self):
        records=[record(n) for n in range(257)]
        self.s.configure_schedules(records,5,True);self.s.receipt(records[:256])
        due=self.s.enqueue_due();self.assertEqual([i['purchase_id'] for i in due['items']],['p256'])

    def test_disable_after_enqueued_skips_without_starting_platform_and_item_failures_isolated(self):
        records=[record(2),record(1),record(3)]
        self.s.configure_schedules(records,5,True);job=self.s.enqueue_due()
        self.s.configure_schedules([record(2)],5,False)
        called=[]
        class Client:
            def request(self,action,**kw):
                if action=='configuration':return {}
                raise AssertionError(action)
        def receipt(runner,item,*args):
            called.append(item['purchase_id'])
            if item['purchase_id']=='p1':raise ValueError('资料需核对')
            return 'OBSERVED',{'summary':{'status':'待收货'}}
        with patch.object(Runner,'receipt_item',receipt),patch('aoxiang_receipt_scope.assess',side_effect=lambda path,items,**kw:{i['key']:{'eligible':True,'reason':''} for i in items}):
            out=Runner(self.s,Client(),self.root,self.root/'unused.db',cancel=threading.Event()).run(job['id'])
        self.assertEqual(called,['p1','p3'])
        self.assertEqual([i['state'] for i in out['items']],['SKIPPED','REVIEW','OBSERVED'])
        self.assertEqual(out['state'],'COMPLETED_WITH_ERRORS')
        self.assertTrue(self.s.schedule('p1')['needs_review'])

    def test_pause_in_poll_targets_original_task_and_prevents_next_store(self):
        cancel=threading.Event();calls=[]
        class Client:
            def request(self,action,**kw):
                calls.append((action,kw))
                if action=='receipt.status':cancel.set();return {'active':True,'state':'RUNNING'}
                if action=='receipt.pause':return {'state':'PAUSED'}
                raise AssertionError(action)
        runner=Runner(self.s,Client(),self.root,self.root/'unused.db',cancel=cancel,delay=.001)
        with self.assertRaises(Cancelled):runner.wait_status('receipt.status','purchase_id','original',cancel_action='receipt.pause')
        self.assertEqual(calls,[('receipt.status',{'purchase_id':'original'}),('receipt.pause',{'purchase_id':'original'})])


class RunnerTests(unittest.TestCase):
    def setUp(self):
        from test_aoxiang_purchase_tasks import PurchaseTaskTests
        self.f=PurchaseTaskTests();self.f.setUp();self.store=Workflows(self.f.root)
    def tearDown(self):self.f.tearDown()

    def runner(self,client,cancel=None):return Runner(self.store,client,self.f.client.component_root,self.f.f.path,cancel=cancel or threading.Event(),delay=.001)

    def test_batch_template_once_actual_native_package_and_real_component_reservation(self):
        real=self.f.client;calls=[];f=self.f
        class Client:
            def request(self,action,**kw):
                calls.append((action,kw))
                if action=='template.start':return {'id':'tpl','state':'RUNNING'}
                if action=='template.status':return {'id':'tpl','state':'READY','receipt':f.receipt}
                return real.request(action,**kw)
        job=self.store.prepare([self.f.f.h]);before=self.f.f.old_tables()
        result=self.runner(Client()).run(job['id'])
        self.assertEqual(result['state'],'READY');self.assertEqual(result['items'][0]['state'],'PREPARED')
        self.assertEqual(sum(a=='template.start' for a,_ in calls),1)
        self.assertFalse(any(a=='purchase.start' for a,_ in calls))
        p=result['items'][0]['package'];self.assertNotIn('intent',p)
        self.assertEqual(real.request('purchase.status',purchase_id=p['id'])['state'],'RESERVED')
        self.assertEqual(self.f.f.old_tables(),before)
        self.runner(Client()).run(job['id']);self.assertEqual(sum(a=='template.start' for a,_ in calls),1)

    def test_unknown_upload_response_is_not_replayed_and_payment_stale_rejected_before_upload(self):
        p=self.f.package();self.f.reserve(p)
        j=self.store.prepare([self.f.f.h]);self.store.update(j['id'],position=0,changes={'state':'PREPARED','package':{k:p[k] for k in ('id','package_file','package_sha256','plan_file','plan_sha256')}})
        ready=self.store.update(j['id'],state='READY');job=self.store.upload(j['id'],[0],ready['sequence'])
        real=self.f.client;calls=[]
        class Client:
            def request(self,action,**kw):
                calls.append(action)
                if action=='purchase.start':raise ComponentError('RESPONSE_UNKNOWN','unknown')
                return real.request(action,**kw)
        with self.assertRaises(ComponentError):self.runner(Client()).run(job['id'])
        self.assertEqual(calls.count('purchase.start'),1)
        self.assertEqual(self.store.get(job['id'])['items'][0]['state'],'PENDING')
        self.f.f.save('payment',[{'付款单商品数量':8,'实际金额':.99}]);calls.clear()
        result=self.runner(Client()).run(job['id'])
        self.assertEqual(result['items'][0]['state'],'REVIEW');self.assertNotIn('purchase.start',calls)


class ReceiptBatchTests(unittest.TestCase):
    def test_formal_receipt_saved_then_ack_lost_reuses_same_observation_no_duplicate_receipt(self):
        from test_aoxiang_receipts import ReceiptTests
        from aoxiang_directory import atomic_json
        f=ReceiptTests();f.setUp()
        try:
            s=Workflows(f.f.root);p=f.package;id=p['id']
            index=s.folder/'purchase-packages'/id/'本地任务索引.json';index.parent.mkdir(parents=True)
            atomic_json(index,p)
            m=f.manifest(f.observation());basis=p['intent']['basis']
            r={'id':id,'basis':basis,'binding':{'orderId':'PO1'}}
            s.configure_schedules([r],5,True);job=s.enqueue_due();old=f.f.f.view()['documents']
            calls=[]
            class Client:
                lose=True
                def request(self,action,**kw):
                    calls.append(action)
                    if action=='configuration':return f.f.config
                    state={'id':id,'scope':{'orderId':'PO1'},'basis':basis,'terminal':True,'state':'READY','manifest':m}
                    if action=='receipt.status':return state
                    if action=='receipt.synced':
                        if self.lose:self.lose=False;raise ComponentError('RESPONSE_UNKNOWN','ack lost')
                        return {**state,'state':'SYNCED','hostReceipt':kw['receipt']}
                    raise AssertionError(action)
            client=Client()
            runner=Runner(s,client,f.f.client.component_root,f.f.f.path,cancel=threading.Event(),delay=.001)
            with self.assertRaises(ComponentError):runner.run(job['id'])
            self.assertEqual(f.f.f.view()['documents']['receipt']['version'],1)
            out=runner.run(job['id']);self.assertEqual(out['state'],'COMPLETED')
            self.assertEqual(f.f.f.view()['documents']['receipt']['version'],1)
            self.assertFalse(s.schedule(id)['enabled']);self.assertFalse(s.schedule(id)['final_pending'])
            for stage in ('payment','return'):self.assertEqual(f.f.f.view()['documents'][stage],old[stage])
            self.assertNotIn('receipt.start',calls);self.assertNotIn('purchase.start',calls)
        finally:f.tearDown()


if __name__=='__main__':unittest.main()
