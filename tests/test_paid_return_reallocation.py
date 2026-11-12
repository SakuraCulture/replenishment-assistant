"""Paid -> amended return -> original-batch correction, on isolated databases."""
import copy
import sqlite3
import threading
import unittest
from contextlib import closing

import test_jst as fixture
import jst_orders as orders
import jst_batches as batches
import jst_results as results
import jst_exports as exports
import order_lifecycle as lifecycle
import lifecycle_io
import work_repository as wr
from runtime_support import Cancelled


class PaidReturnReallocation(unittest.TestCase):
    def setUp(self):
        self.f=fixture.Service();self.f.setUp();self.path=self.f.path;self.head=self.f.h
    def tearDown(self):self.f.tearDown()
    def doc(self,head,stage):
        with closing(sqlite3.connect(self.path)) as db:return lifecycle.current(db,head,stage)
    def active(self,head=None):
        with closing(sqlite3.connect(self.path)) as db:return orders.active(db,head or self.head)
    def save(self,head,stage,quantity,sku='0001'):
        previous=self.f.f.h;self.f.f.h=head
        try:return self.f.f.save(stage,[{'门店编码':head['store'],'供货仓':head['warehouse'],'商品编码':sku,lifecycle_io.QUANTITIES[stage]:quantity}])
        finally:self.f.f.h=previous
    def paid(self,heads=None):
        plan=batches.preview_new(self.path,heads or [self.head],{'河北仓':fixture.catalog()})
        receipt=batches.execute(self.path,plan);self.assertFalse(receipt['errors'])
        self.bid=receipt['batch_id'];self.old_gid=self.active();self.old_generation=orders.get(self.path,self.old_gid)
        lifecycle.commit(self.path,orders.payment_plan(self.path,self.old_gid))
        self.old_payment=self.doc(self.head,'payment')
        self.assertEqual(self.f.f.view()['status'],'待收货')
        return receipt
    def change_return(self,quantity=30):return self.save(self.head,'return',quantity)
    def preview(self):return batches.preview_existing(self.path,self.bid,[self.head],True)
    def read(self):return results.read(self.path,self.f.f.root/'result-cache.sqlite3',self.head['day'],scope=[results.key(self.head)])
    def assert_originals(self):
        self.assertEqual(orders.get(self.path,self.old_gid),self.old_generation)
        self.assertEqual(self.doc(self.head,'payment'),self.old_payment)

    def test_paid_new_return_correct_export_and_recheck_payment(self):
        self.paid();self.change_return();data=self.read()
        self.assertEqual(data['counts']['stale'],1);self.assertEqual(results.export_plan(data)['entries'],[])
        self.assertIn('旧付款保留',data['records'][0]['reason'])
        plan=self.preview();self.assertEqual(plan['payload']['plans'][0]['previous_payment'],lifecycle.rules.basis_token(self.old_payment))
        receipt=batches.execute(self.path,plan);self.assertFalse(receipt['errors']);gid=receipt['generated'][0]
        self.assertNotEqual(gid,self.old_gid);self.assertEqual(orders.get(self.path,gid)['summary']['quantity'],'30')
        self.assert_originals();self.assertEqual(self.f.f.view()['status'],'待付款')
        exported=exports.export_checked(self.path,[gid],self.f.f.root/'out');self.assertFalse(exported['failed'])
        import openpyxl
        book=openpyxl.load_workbook(exported['files'][0]['path'],read_only=True)
        try:self.assertEqual(list(book.active.values)[1],('0001',2,30,self.head['number']))
        finally:book.close()
        new_payment=orders.payment_plan(self.path,gid);lifecycle.commit(self.path,new_payment)
        self.assertEqual(self.f.f.view()['status'],'待收货');self.assertEqual(self.doc(self.head,'payment')['version'],2)
        self.assertEqual(lifecycle.history(self.path,self.head,'payment')[-1],self.old_payment)
        self.assertEqual(orders.get(self.path,self.old_gid),self.old_generation)

    def test_selected_correction_keeps_peer_reservation_and_stock_cap(self):
        other=self.f.second_store();receipt=self.paid([self.head,other]);other_gid=self.active(other);frozen=orders.get(self.path,other_gid)
        for demand,expected in [(20,'20'),(80,'40')]:
            with self.subTest(demand=demand):
                self.change_return(demand);plan=self.preview();self.assertEqual(plan['payload']['plans'][0]['summary']['quantity'],expected)
                r=batches.execute(self.path,plan);self.assertFalse(r['errors']);self.assertEqual(self.active(other),other_gid)
                self.assertEqual(orders.get(self.path,other_gid),frozen);self.assert_originals()
                with closing(sqlite3.connect(self.path)) as db:
                    self.assertEqual(batches.reservations(db,self.bid,'河北仓')['0001'],int(expected)+60)

    def test_zero_amendment_releases_only_selected_reservation(self):
        other=self.f.second_store();self.paid([self.head,other]);other_gid=self.active(other)
        self.change_return(0);plan=self.preview();self.assertEqual(plan['payload']['plans'][0]['summary']['positive'],0)
        r=batches.execute(self.path,plan);self.assertFalse(r['errors']);self.assertEqual(r['zero'],[self.head['number']]);self.assertIsNone(self.active())
        with closing(sqlite3.connect(self.path)) as db:self.assertEqual(batches.reservations(db,self.bid,'河北仓')['0001'],60)
        self.assertEqual(self.active(other),other_gid);self.assert_originals();self.assertEqual(self.read()['counts']['zero'],1)

    def test_same_store_other_warehouse_payment_and_generation_are_unchanged(self):
        import purchases
        import warehouse_catalog
        sd=next(h for h in purchases.targets(self.path) if h['store']==self.head['store'] and h['warehouse']=='山东仓')
        self.save(sd,'return',40,'0002');catalog=fixture.catalog('山东仓',stock=60)
        item=catalog['items'].pop('0001');item['商品编码']='0002';catalog['items']['0002']=item;catalog['revision']=warehouse_catalog.revision(catalog['items'])
        p=batches.preview_new(self.path,[self.head,sd],{'河北仓':fixture.catalog(),'山东仓':catalog});r=batches.execute(self.path,p);self.assertFalse(r['errors'])
        self.bid=r['batch_id'];self.old_gid=self.active();self.old_generation=orders.get(self.path,self.old_gid)
        sd_gid=self.active(sd);sd_generation=orders.get(self.path,sd_gid)
        for gid in (self.old_gid,sd_gid):lifecycle.commit(self.path,orders.payment_plan(self.path,gid))
        self.old_payment=self.doc(self.head,'payment');sd_payment=self.doc(sd,'payment');self.change_return()
        corrected=batches.execute(self.path,self.preview());self.assertFalse(corrected['errors'])
        self.assertEqual(self.active(sd),sd_gid);self.assertEqual(orders.get(self.path,sd_gid),sd_generation);self.assertEqual(self.doc(sd,'payment'),sd_payment)
        self.assertEqual(lifecycle.snapshot(self.path,sd)['status'],'待收货');self.assert_originals()

    def test_current_payment_still_blocks_arbitrary_reallocation(self):
        self.paid()
        with self.assertRaisesRegex(ValueError,'已有付款记录'):self.preview()
        self.assertEqual(self.active(),self.old_gid);self.assert_originals()

    def test_zero_then_positive_return_can_continue_in_same_original_batch(self):
        self.paid();self.change_return(0);r=batches.execute(self.path,self.preview());self.assertFalse(r['errors'])
        self.change_return(25);data=self.read();record=data['records'][0]
        self.assertIsNone(record['gid']);self.assertTrue(record['original_zero']);self.assertEqual(record['batch_id'],self.bid);self.assertEqual(record['state'],'stale')
        r=batches.execute(self.path,self.preview());self.assertFalse(r['errors']);self.assertEqual(orders.get(self.path,r['generated'][0])['summary']['quantity'],'25')
        self.assert_originals()

    def test_old_receipt_preview_cannot_save_after_return_correction(self):
        self.paid();old_receipt=self.f.f.plan(self.f.f.scan('receipt',[{'实际收货数量':5}]))
        self.change_return();r=batches.execute(self.path,self.preview());self.assertFalse(r['errors'])
        with self.assertRaisesRegex(wr.Conflict,'上游'):lifecycle.commit(self.path,old_receipt)
        self.assertIsNone(self.doc(self.head,'receipt'));self.assert_originals()

    def test_identical_return_reimport_does_not_invalidate_payment(self):
        scan=self.f.f.scan('return',[{'门店确认数量':40}]);lifecycle.commit(self.path,self.f.f.plan(scan));self.paid()
        before=self.doc(self.head,'return');same=self.f.f.plan(scan)
        self.assertFalse(same['plans'][0]['changed']);lifecycle.commit(self.path,same)
        self.assertEqual(self.doc(self.head,'return'),before);self.assertEqual(self.read()['counts']['ready'],1)
        with self.assertRaisesRegex(ValueError,'已有付款记录'):self.preview()

    def test_receipt_fact_even_stale_is_not_reallocated(self):
        self.paid();self.save(self.head,'receipt',5);before=self.doc(self.head,'receipt');self.change_return()
        with self.assertRaisesRegex(ValueError,'已有收货记录'):self.preview()
        self.assertEqual(self.doc(self.head,'receipt'),before);self.assertEqual(self.active(),self.old_gid);self.assert_originals()

    def test_lock_still_blocks_return_change_and_correction(self):
        self.paid();orders.lock(self.path,[self.old_gid])
        with self.assertRaisesRegex(Exception,'锁定'):self.change_return()
        with self.assertRaisesRegex(ValueError,'已锁单'):self.preview()
        self.assert_originals()

    def test_pending_and_legacy_payments_do_not_gain_correction_permission(self):
        self.paid();self.change_return()
        for override in ({'pending':True},{'schema':1}):
            with self.subTest(override=override),closing(sqlite3.connect(self.path)) as db,db:
                p=copy.deepcopy(self.old_payment['payload']);p.update(override)
                db.execute("UPDATE lifecycle_versions SET payload=?,digest=? WHERE stage='payment'",(wr.dumps(p),lifecycle.fingerprint(p)))
            with self.assertRaisesRegex(ValueError,'已有付款记录'):self.preview()
        self.assertEqual(self.active(),self.old_gid)

    def test_rechecked_payment_after_preview_rejects_commit(self):
        self.paid();self.change_return();plan=self.preview();self.save(self.head,'payment',30);payment=self.doc(self.head,'payment')
        r=batches.execute(self.path,plan);self.assertTrue(r['errors']);self.assertEqual(self.active(),self.old_gid)
        self.assertEqual(self.doc(self.head,'payment'),payment)

    def test_receipt_after_preview_rejects_commit(self):
        self.paid();self.change_return();plan=self.preview();self.save(self.head,'receipt',5);receipt=self.doc(self.head,'receipt')
        r=batches.execute(self.path,plan);self.assertTrue(r['errors']);self.assertIn('已有收货记录',r['errors'][0]['error'])
        self.assertEqual(self.active(),self.old_gid);self.assertEqual(self.doc(self.head,'receipt'),receipt);self.assert_originals()

    def test_return_after_preview_rejects_commit(self):
        self.paid();self.change_return();plan=self.preview();self.change_return(25)
        r=batches.execute(self.path,plan);self.assertTrue(r['errors']);self.assertIn('回传依据已变化',r['errors'][0]['error'])
        self.assertEqual(self.active(),self.old_gid);self.assert_originals()

    def test_plan_without_frozen_payment_token_cannot_bypass_guard(self):
        self.paid();self.change_return();plan=self.preview();del plan['payload']['plans'][0]['previous_payment'];plan['digest']=orders.digest(plan['payload'])
        r=batches.execute(self.path,plan);self.assertTrue(r['errors']);self.assertIn('付款依据已变化',r['errors'][0]['error'])
        self.assertEqual(self.active(),self.old_gid);self.assert_originals()

    def test_correction_failure_rolls_back_all_selected_orders(self):
        other=self.f.second_store();self.paid([self.head,other]);other_gid=self.active(other)
        lifecycle.commit(self.path,orders.payment_plan(self.path,other_gid));self.change_return();self.save(other,'return',50)
        plan=batches.preview_existing(self.path,self.bid,[self.head,other],True)
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute("CREATE TRIGGER fail_correction BEFORE INSERT ON jst_generations WHEN NEW.oid='oid-u' BEGIN SELECT RAISE(ABORT,'injected-correction-failure'); END")
        r=batches.execute(self.path,plan);self.assertTrue(r['errors']);self.assertEqual(r['generated'],[])
        self.assertEqual(self.active(),self.old_gid);self.assertEqual(self.active(other),other_gid);self.assert_originals()
        with closing(sqlite3.connect(self.path)) as db:self.assertEqual(batches.reservations(db,self.bid,'河北仓')['0001'],100)

    def test_cancel_before_submit_keeps_original_allocation_and_payment(self):
        self.paid();self.change_return();plan=self.preview();stop=threading.Event();stop.set()
        with self.assertRaises(Cancelled):batches.execute(self.path,plan,cancel=stop)
        self.assertEqual(self.active(),self.old_gid);self.assert_originals()

    def test_retry_does_not_duplicate_allocation_or_payment(self):
        self.paid();self.change_return();plan=self.preview();first=batches.execute(self.path,plan);again=batches.execute(self.path,plan)
        self.assertFalse(first['errors']);self.assertFalse(again['errors']);self.assertEqual(first['generated'],again['generated']);self.assert_originals()
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM jst_generations').fetchone()[0],2)
            self.assertEqual(batches.reservations(db,self.bid,'河北仓')['0001'],30)

    def test_no_payment_correction_keeps_existing_contract(self):
        r=batches.execute(self.path,batches.preview_new(self.path,[self.head],{'河北仓':fixture.catalog()}));self.bid=r['batch_id']
        self.change_return();plan=self.preview();self.assertNotIn('previous_payment',plan['payload']['plans'][0])
        corrected=batches.execute(self.path,plan);self.assertFalse(corrected['errors']);self.assertIsNone(self.doc(self.head,'payment'))


if __name__=='__main__':unittest.main()
