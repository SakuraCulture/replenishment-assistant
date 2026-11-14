"""Pending-batch recovery must preserve generated and unselected warehouse orders."""
import copy,sqlite3,threading,unittest
from contextlib import closing
from unittest.mock import patch
import test_jst as fixture
import jst_batches as batches,jst_orders as orders,work_repository as wr
from runtime_support import Cancelled


class PendingRecovery(unittest.TestCase):
    def setUp(self):
        self.f=fixture.Service();self.f.setUp();self.path=self.f.path;self.head=self.f.h
        self.other=self.f.second_store()
        self.plan=batches.preview_new(self.path,[self.head,self.other],{'河北仓':fixture.catalog(stock=100)})
        self.prepared=batches.register(self.path,self.plan);self.bid=self.plan['payload']['batch_id']
    def tearDown(self):self.f.tearDown()
    def claims(self,heads=None):return batches.unfinished(self.path,heads or [self.head])
    def context(self):
        with closing(sqlite3.connect(self.path)) as db:return batches.context(db,self.bid)
    def test_current_preview_blocks_but_identifies_exact_recovery_scope(self):
        before=self.context();claims=self.claims()
        with self.assertRaises(wr.Conflict):batches.preview_new(self.path,[self.head],{'河北仓':fixture.catalog(stock=700)})
        self.assertEqual(self.context(),before);self.assertEqual(len(claims),1)
        self.assertEqual(claims[0]['batch_id'],self.bid)
        self.assertEqual([m['head']['oid'] for m in claims[0]['selected']],[self.head['oid']])
        self.assertEqual(len(claims[0]['remaining']),2)
    def test_release_selected_then_fresh_preview_keeps_other_frozen_batch(self):
        claims=self.claims();before=self.context()
        self.assertEqual(batches.release_unfinished(self.path,claims),1)
        after=self.context();self.assertEqual(after['stocks'],before['stocks'])
        self.assertEqual([m['status'] for m in after['members']],['cancelled','pending'])
        self.assertEqual(after['members'][1],before['members'][1]);self.assertEqual(after['status'],'paused')
        fresh=batches.preview_new(self.path,[self.head],{'河北仓':fixture.catalog(stock=700)})
        self.assertEqual(fresh['payload']['plans'][0]['rows'][0]['stock'],'700')
        result=batches.execute(self.path,fresh);self.assertFalse(result['errors']);self.assertEqual(len(result['generated']),1)
        old=batches.continue_batch(self.path,self.bid);self.assertFalse(old['errors'])
        self.assertEqual(orders.get(self.path,old['generated'][0])['summary']['quantity'],'80')
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM jst_generations WHERE oid=?',(self.head['oid'],)).fetchone()[0],1)
    def test_released_old_preview_and_queue_cannot_restore_selected_member(self):
        batches.release_unfinished(self.path,self.claims([self.head,self.other]))
        with self.assertRaises(wr.Conflict):batches.execute(self.path,self.prepared)
        self.assertEqual(batches.drain(self.path),[])
        with closing(sqlite3.connect(self.path)) as db:self.assertIsNone(orders.active(db,self.head))
        self.assertEqual(self.context()['status'],'complete')
    def test_changed_claim_rejected_and_generated_order_preserved(self):
        claims=self.claims([self.head,self.other]);stop=threading.Event()
        def progress(value):
            if getattr(value,'business_event',None):stop.set()
        result=batches.execute(self.path,self.prepared,progress=progress,cancel=stop)
        self.assertEqual(len(result['generated']),1);before=self.context()
        with self.assertRaises(wr.Conflict):batches.release_unfinished(self.path,claims)
        self.assertEqual(self.context(),before)
        self.assertEqual(orders.get(self.path,result['generated'][0])['summary']['quantity'],'40')
        self.assertEqual(batches.release_unfinished(self.path,self.claims([self.head,self.other])),1)
        self.assertEqual(self.context()['members'][0]['status'],'done')
    def test_failure_second_member_rolls_back_first(self):
        claims=self.claims([self.head,self.other]);before=self.context()
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute("CREATE TRIGGER fail_release BEFORE UPDATE OF status ON jst_batch_members WHEN NEW.oid='oid-u' AND NEW.status='cancelled' BEGIN SELECT RAISE(ABORT,'injected release failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):batches.release_unfinished(self.path,claims)
        self.assertEqual(self.context(),before)
    def test_cancel_has_no_persistent_effect(self):
        claims=self.claims();before=self.context();stop=threading.Event();stop.set()
        with self.assertRaises(Cancelled):batches.release_unfinished(self.path,claims,cancel=stop)
        self.assertEqual(self.context(),before)
    def test_old_release_request_cannot_cancel_replacement_batch(self):
        claims=self.claims();batches.release_unfinished(self.path,claims)
        new=batches.register(self.path,batches.preview_new(self.path,[self.head],{'河北仓':fixture.catalog(stock=500)}))
        with self.assertRaises(wr.Conflict):batches.release_unfinished(self.path,claims)
        result=batches.execute(self.path,new);self.assertFalse(result['errors'])
        self.assertEqual(orders.get(self.path,result['generated'][0])['summary']['quantity'],'40')
    def test_same_parent_other_warehouse_claim_is_preserved(self):
        import purchases,warehouse_catalog as wc
        sd=next(h for h in purchases.targets(self.path) if h['oid']==self.head['oid'] and h['warehouse']=='山东仓')
        old=self.f.f.h;self.f.f.h=sd
        try:self.f.f.save('return',[{'供货仓':'山东仓','商品编码':'0002','门店确认数量':30}])
        finally:self.f.f.h=old
        catalog=fixture.catalog('山东仓',stock=70);item=catalog['items'].pop('0001');item['商品编码']='0002';catalog['items']['0002']=item;catalog['revision']=wc.revision(catalog['items'])
        plan=batches.preview_new(self.path,[sd],{'山东仓':catalog});batches.register(self.path,plan)
        with closing(sqlite3.connect(self.path)) as db:before=batches.context(db,plan['payload']['batch_id'])
        batches.release_unfinished(self.path,self.claims())
        with closing(sqlite3.connect(self.path)) as db:self.assertEqual(batches.context(db,plan['payload']['batch_id']),before)
        done=batches.continue_batch(self.path,plan['payload']['batch_id']);self.assertFalse(done['errors'])
        self.assertEqual(orders.get(self.path,done['generated'][0])['summary']['quantity'],'30')
    def test_cancel_while_waiting_for_writer_preserves_claims(self):
        import time
        claims=self.claims();before=self.context();stop=threading.Event();errors=[]
        db=sqlite3.connect(self.path);db.execute('BEGIN IMMEDIATE')
        def release():
            try:batches.release_unfinished(self.path,claims,cancel=stop)
            except Exception as exc:errors.append(exc)
        t=threading.Thread(target=release);t.start();time.sleep(.2);stop.set();t.join(4);db.rollback();db.close()
        self.assertFalse(t.is_alive());self.assertEqual(len(errors),1);self.assertIsInstance(errors[0],Cancelled)
        self.assertEqual(self.context(),before)

if __name__=='__main__':unittest.main()
