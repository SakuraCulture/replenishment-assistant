"""Single-order number edits must not modify business documents."""
import copy
import sqlite3
import threading
import unittest
from contextlib import closing
from unittest.mock import patch
import test_jst_payment as fixtures
import payment_references as refs
import order_lifecycle as lc
import jst_orders as jst
import work_repository as wr
from runtime_support import Cancelled, CommitProgress


class PaymentReferenceTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.DirectPayment(); self.f.setUp(); self.path=self.f.path
        self.gid=self.f.gids[0]; self.head=jst.get(self.path,self.gid)['head']
        lc.commit(self.path,jst.payment_plan(self.path,self.gid))

    def tearDown(self):self.f.tearDown()

    def frozen(self):
        with closing(sqlite3.connect(self.path)) as db:
            return {t:db.execute('SELECT * FROM '+t+' ORDER BY rowid').fetchall() for t in ('normal_orders','normal_revisions','lifecycle_versions','lifecycle_files','jst_generations','jst_active','jst_reservations','jst_locks')}

    def metadata(self):
        with closing(sqlite3.connect(self.path)) as db:
            return [db.execute('SELECT * FROM '+t+' ORDER BY rowid').fetchall() for t in ('lifecycle_mappings','lifecycle_events')]

    def stage(self,stage='payment',number='NATIVE'):
        with closing(sqlite3.connect(self.path)) as db:
            payment=lc.current(db,self.head,'payment'); rows=copy.deepcopy(payment['payload']['raw_rows'])
            for r in rows:r['document_number']=number
            plan=lc.make_plan(db,lc.full_head(self.path,self.head),stage,rows,[])
        return lc.commit(self.path,{'plans':[plan],'files':[]})

    def test_fill_and_correct_keep_every_business_document_identical(self):
        original=self.frozen(); initial=refs.read(self.path,self.head); self.assertEqual(initial['number'],'')
        self.assertTrue(refs.save(self.path,initial,' 00994021108001 ')['changed'])
        current=refs.read(self.path,self.head); self.assertEqual(current['number'],'00994021108001')
        refs.save(self.path,current,'00994021108002'); current=refs.read(self.path,self.head)
        self.assertEqual(current['history'][0][1:],['00994021108001','00994021108002'])
        self.assertEqual(current['history'][1][1:],['未填写','00994021108001']); self.assertEqual(self.frozen(),original)

    def test_repeat_is_noop_and_read_only_open_does_not_write(self):
        before=self.path.read_bytes(); initial=refs.read(self.path,self.head); self.assertEqual(self.path.read_bytes(),before)
        refs.save(self.path,initial,'J1'); prior=self.metadata()
        self.assertFalse(refs.save(self.path,refs.read(self.path,self.head),'J1')['changed']); self.assertEqual(self.metadata(),prior)

    def test_existing_imported_number_prefills_without_modifying_original(self):
        self.stage(); original=self.frozen(); initial=refs.read(self.path,self.head)
        self.assertEqual(initial['number'],'NATIVE'); self.assertEqual(initial['source'],'原付款文件')
        refs.save(self.path,initial,'CORRECTED'); self.assertEqual(self.frozen(),original)
        self.assertEqual(refs.read(self.path,self.head)['number'],'CORRECTED')

    def test_same_store_other_warehouse_and_unselected_orders_untouched(self):
        others=[jst.get(self.path,gid)['head'] for gid in self.f.gids if gid!=self.gid]
        for gid in self.f.gids:lc.commit(self.path,jst.payment_plan(self.path,gid))
        refs.save(self.path,refs.read(self.path,self.head),'J1')
        self.assertTrue(all(refs.read(self.path,h)['number']=='' for h in others))

    def test_can_edit_after_receipt_and_keeps_receipt_basis(self):
        self.stage('receipt','RECEIPT1'); original=self.frozen(); view=lc.snapshot(self.path,self.head)
        refs.save(self.path,refs.read(self.path,self.head),'JST1')
        self.assertEqual(self.frozen(),original); self.assertEqual(lc.snapshot(self.path,self.head),view)

    def test_invalid_number_does_not_create_a_record(self):
        initial=refs.read(self.path,self.head); before=self.metadata()
        for number in ('','  ','A B','A\nB','A\0B','x'*101):
            with self.assertRaises(ValueError):refs.save(self.path,initial,number)
        self.assertEqual(self.metadata(),before)

    def test_concurrent_number_or_payment_update_requires_reopen(self):
        initial=refs.read(self.path,self.head); refs.save(self.path,initial,'OTHER')
        with self.assertRaises(wr.Conflict):refs.save(self.path,initial,'STALE')
        current=refs.read(self.path,self.head); self.stage(number='NEW-PAYMENT')
        with self.assertRaises(wr.Conflict):refs.save(self.path,current,'STALE')
        self.assertEqual(refs.read(self.path,self.head)['number'],'OTHER')

    def test_concurrent_update_between_read_and_write_is_detected(self):
        initial=refs.read(self.path,self.head)
        def concurrently(*_):
            with patch('storage_chunks.backup_before_write'):
                refs.save(self.path,initial,'OTHER')
        with patch('storage_chunks.backup_before_write',side_effect=concurrently):
            with self.assertRaises(wr.Conflict):refs.save(self.path,initial,'STALE')
        self.assertEqual(refs.read(self.path,self.head)['number'],'OTHER')

    def test_deleted_or_resigned_order_cannot_be_edited(self):
        initial=refs.read(self.path,self.head)
        with closing(sqlite3.connect(self.path)) as db,db:db.execute('DELETE FROM normal_orders WHERE oid=?',(self.head['oid'],))
        with self.assertRaises(wr.Conflict):refs.save(self.path,initial,'STALE')
        self.assertEqual(self.metadata()[0],[])

    def test_payment_required_and_cancellation_before_write(self):
        other=jst.get(self.path,self.f.gids[-1])['head']
        with self.assertRaisesRegex(ValueError,'先确认付款'):refs.read(self.path,other)
        initial=refs.read(self.path,self.head); before=self.metadata(); event=threading.Event(); event.set()
        with self.assertRaises(Cancelled):refs.save(self.path,initial,'J1',cancel=event)
        self.assertEqual(self.metadata(),before)

    def test_mid_write_failure_rolls_back_number_and_history_together(self):
        initial=refs.read(self.path,self.head); before=self.metadata()
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute("CREATE TRIGGER fail_ref BEFORE INSERT ON lifecycle_events WHEN NEW.action='维护聚水潭内部订单号' BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError,'injected'):refs.save(self.path,initial,'J1')
        self.assertEqual(self.metadata(),before)

    def test_commit_is_not_interrupted_halfway(self):
        event=threading.Event()
        def progress(value):
            if isinstance(value,CommitProgress):event.set()
        self.assertTrue(refs.save(self.path,refs.read(self.path,self.head),'J1',progress,event)['changed'])
        self.assertEqual(len(refs.read(self.path,self.head)['history']),1)


if __name__=='__main__':unittest.main()
