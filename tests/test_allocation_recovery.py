"""Real isolated inventory transactions with faults at control/commit boundaries."""
import copy
import sqlite3
import sys
import threading
import unittest
from contextlib import closing
from unittest.mock import patch
import test_jst as fixture
import jst_batches as b
import jst_orders as j
import order_lifecycle as lc
import jst_tasks


class AllocationRecovery(unittest.TestCase):
    def setUp(self):
        self.f=fixture.Service();self.f.setUp();self.path=self.f.path
    def tearDown(self):self.f.tearDown()
    def plan(self,heads=None):return b.preview_new(self.path,heads or [self.f.h],{'河北仓':fixture.catalog()})
    def state(self,bid):
        with closing(sqlite3.connect(self.path)) as db:return b.context(db,bid)
    def counts(self):
        with closing(sqlite3.connect(self.path)) as db:
            return tuple(db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('jst_generations','jst_reservations','jst_steps'))
    def test_before_first_allocation_failure_restores_only_original_batch(self):
        old=self.plan();bid=old['payload']['batch_id'];b.register(self.path,old)
        other=self.f.second_store();new=self.plan([other])
        with patch.object(b,'activate',side_effect=OSError('before first allocation')):
            with self.assertRaisesRegex(OSError,'before first'):b.execute(self.path,new)
        self.assertEqual(self.state(bid)['status'],'active')
        self.assertEqual(self.state(new['payload']['batch_id'])['status'],'paused')
        self.assertEqual(self.counts(),(0,0,0));self.assertEqual(len(b.drain(self.path)[0]['generated']),1)
    def test_newer_manual_pause_is_never_undone(self):
        old=self.plan();bid=old['payload']['batch_id'];b.register(self.path,old)
        other=self.f.second_store();new=self.plan([other])
        def fail(*a,**k):
            b.pause(self.path,bid)
            raise OSError('new manual decision')
        with patch.object(b,'activate',side_effect=fail),self.assertRaises(OSError):b.execute(self.path,new)
        self.assertEqual(self.state(bid)['status'],'paused')
    def test_finalization_failure_returns_committed_receipt(self):
        p=self.plan();begin=lc.begin;fired=[]
        def fail(db,cancel,**kw):
            if sys._getframe(1).f_code.co_name=='_execute' and not fired:
                fired.append(True);raise sqlite3.OperationalError('finalization write failed')
            return begin(db,cancel,**kw)
        with patch.object(lc,'begin',fail):r=b.execute(self.path,p)
        self.assertTrue(fired);self.assertTrue(r['finalization_pending']);self.assertEqual(r['commit_state'],'committed')
        self.assertEqual(len(r['generated']),1);self.assertEqual(self.counts(),(1,1,1))
        repaired=b.recover_batch(self.path,r['batch_id'])
        self.assertEqual(repaired['generated'],r['generated']);self.assertEqual(self.state(r['batch_id'])['status'],'complete')
        self.assertEqual(self.counts(),(1,1,1))
        self.assertEqual(b.recover_batch(self.path,r['batch_id'])['generated'],r['generated'])
        self.assertEqual(b.continue_batch(self.path,r['batch_id'])['generated'],r['generated'])
    def test_background_tick_can_finish_control_state_without_allocating_again(self):
        p=self.plan();r=b.execute(self.path,p);bid=r['batch_id']
        with closing(sqlite3.connect(self.path)) as db,db:db.execute("UPDATE jst_batches SET status='active' WHERE batch_id=?",(bid,))
        before=self.counts();result=b.drain(self.path)
        self.assertEqual(result[0]['generated'],r['generated']);self.assertEqual(self.counts(),before)
        self.assertEqual(self.state(bid)['status'],'complete')
    def test_partial_commit_keeps_previous_batch_paused_and_does_not_duplicate(self):
        original=self.plan();old_id=original['payload']['batch_id'];b.register(self.path,original)
        other=self.f.second_store();p=self.plan([other]);stop=threading.Event()
        def saved(value):
            if getattr(value,'business_event',None):stop.set()
        r=b.execute(self.path,p,progress=saved,cancel=stop)
        self.assertEqual(len(r['generated']),1);self.assertEqual(self.state(old_id)['status'],'paused')
        before=self.counts();b.recover_batch(self.path,r['batch_id']);self.assertEqual(self.counts(),before)
    def test_partial_batch_recovery_leaves_pending_and_keeps_order(self):
        other=self.f.second_store();p=self.plan([self.f.h,other]);stop=threading.Event()
        def saved(value):
            if getattr(value,'business_event',None):stop.set()
        r=b.execute(self.path,p,progress=saved,cancel=stop);self.assertEqual(len(r['generated']),1)
        before=self.state(r['batch_id']);repaired=b.recover_batch(self.path,r['batch_id'])
        self.assertEqual(repaired['pending'],[other['number']]);self.assertEqual(self.state(r['batch_id'])['members'],before['members'])
        next_r=b.continue_batch(self.path,r['batch_id']);self.assertEqual(len(next_r['generated']),1)
        self.assertEqual(self.counts(),(2,2,2))
    def test_cancel_before_register_changes_nothing(self):
        p=self.plan();stop=threading.Event();stop.set()
        from runtime_support import Cancelled
        with self.assertRaises(Cancelled):b.execute(self.path,p,cancel=stop)
        self.assertEqual(b.listing(self.path),[])
    def test_legacy_selected_retry_never_dispatches_other_orders(self):
        other=self.f.second_store();b.register(self.path,self.plan([self.f.h,other]))
        with self.assertRaisesRegex(ValueError,'旧版勾选重试入口'):jst_tasks.retry(self.path,[self.f.h])
        self.assertEqual(self.counts(),(0,0,0))
    def test_recovery_does_not_resume_pending_orders_or_other_batches(self):
        p=self.plan();b.register(self.path,p);bid=p['payload']['batch_id']
        r=b.recover_batch(self.path,bid)
        self.assertEqual(r['generated'],[]);self.assertEqual(r['pending'],[self.f.h['number']])
        self.assertEqual(b.drain(self.path),[]);self.assertEqual(self.counts(),(0,0,0))

    def test_original_zero_recovers_by_batch_even_after_positive_new_batch(self):
        import jst_results
        main=self.f.h;path=self.path
        first=b.execute(path,b.preview_new(path,[main],{'河北仓':fixture.catalog(stock=0)}))
        self.assertEqual(first['zero'],[main['number']])
        second=b.execute(path,self.plan())
        self.assertEqual(len(second['generated']),1);self.assertNotEqual(first['batch_id'],second['batch_id'])
        for legacy in (False,True):
            if legacy:
                with closing(sqlite3.connect(path)) as db,db:
                    db.execute("UPDATE jst_events SET detail=json_remove(detail,'$.batch_id') WHERE action='库存不足未生成'")
            recovered=b.recover_batch(path,first['batch_id'])
            self.assertEqual(recovered['generated'],[]);self.assertEqual(recovered['zero'],first['zero'])
            self.assertFalse(recovered['errors'])
            view=jst_results.read(path,self.f.f.root/'batch-cache.sqlite3',main['day'],batch_id=first['batch_id'])
            self.assertEqual(view['counts']['zero'],1)
            self.assertEqual(view['records'][0]['reason'],'原批分配为0，本单已有其他批次生成结果')
            self.assertEqual(jst_results.export_plan(view)['entries'],[])
        self.assertEqual(b.recover_batch(path,second['batch_id'])['generated'],second['generated'])
