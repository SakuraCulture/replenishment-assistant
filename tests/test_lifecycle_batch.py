"""Batch dependency/order/atomicity contracts; all inputs generated locally."""
import copy,shutil,sqlite3,threading,unittest
from unittest.mock import patch
from contextlib import closing
import test_lifecycle as fixture
import lifecycle_batch as batch,order_lifecycle as lc,lifecycle_io as io,work_repository as wr
from runtime_support import Cancelled

class LifecycleBatch(unittest.TestCase):
    def setUp(self):self.f=fixture.LifecycleTests();self.f.setUp()
    def tearDown(self):self.f.tearDown()
    def documents(self):
        return [batch.group_scan(self.f.scan(s,[{io.QUANTITIES[s]:q}]))[0] for s,q in [('receipt',2),('payment',3),('return',6)]]
    def test_same_batch_is_exactly_sequential_including_all_raw_fields(self):
        docs=self.documents();other=self.f.root/'sequential.sqlite3';shutil.copy2(self.f.path,other)
        p=batch.prepare(self.f.path,docs);before=self.f.old_tables()
        self.assertEqual([x['stage'] for x in p['plans']],list(io.STAGES))
        self.assertFalse(any(x['payload']['pending'] for x in p['plans']))
        lc.commit(self.f.path,p)
        for d in reversed(docs):lc.commit(other,lc.prepare_numbered(other,d['scan']))
        for s in io.STAGES:
            a,b=lc.history(self.f.path,self.f.h,s)[0],lc.history(other,self.f.h,s)[0]
            for k in ('payload','digest','version'):self.assertEqual(a[k],b[k])
        self.assertEqual(self.f.old_tables(),before)
        self.assertEqual(self.f.view()['rows'][0][3:6],[6,3,2])
        again=batch.prepare(self.f.path,docs);self.assertTrue(all(not p['changed'] for p in again['plans']))
    def test_store_warehouse_order_first_seen_then_stage_and_scope(self):
        hb=self.documents();sd=batch.group_scan(self.f.scan('return',[{'补货单号':self.f.heads[1]['number'],'供货仓':'山东仓','商品编码':'0002'}]))[0]
        p=batch.prepare(self.f.path,[sd,*hb],self.f.heads)
        self.assertEqual([(p['head']['warehouse'],p['stage']) for p in p['plans']],[('山东仓','return'),('河北仓','return'),('河北仓','payment'),('河北仓','receipt')])
        with self.assertRaisesRegex(ValueError,'超出'):batch.prepare(self.f.path,[sd],self.f.heads[:1])
    def test_pending_without_upstream_and_exclusion_recomputes_basis(self):
        ds=self.documents();ds[-1]['selected']=False
        p=batch.prepare(self.f.path,ds);self.assertTrue(all(x['payload']['pending'] for x in p['plans']))
        self.assertTrue(all(x['payload']['missing']==[] for x in p['plans']))
    def test_no_summing_duplicate_documents(self):
        ds=self.documents()
        with self.assertRaisesRegex(ValueError,'多份候选'):batch.prepare(self.f.path,[ds[0],copy.deepcopy(ds[0])])
    def test_preview_stale_and_atomic_rollback(self):
        ds=self.documents();p=batch.prepare(self.f.path,ds);self.f.save('return',[{'门店确认数量':11}])
        with self.assertRaises(wr.Conflict):lc.commit(self.f.path,p)
        self.assertEqual(lc.history(self.f.path,self.f.h,'payment'),[])
        self.assertEqual(lc.history(self.f.path,self.f.h,'receipt'),[])
        p=batch.prepare(self.f.path,ds)
        with closing(sqlite3.connect(self.f.path)) as db, db:
            db.execute("CREATE TRIGGER abort_payment BEFORE INSERT ON lifecycle_versions WHEN NEW.stage='payment' BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        old=lc.history(self.f.path,self.f.h,'return')
        with self.assertRaises(sqlite3.IntegrityError):lc.commit(self.f.path,p)
        self.assertEqual(lc.history(self.f.path,self.f.h,'return'),old)
        self.assertEqual(lc.history(self.f.path,self.f.h,'payment'),[])
    def test_cancel_before_and_at_safe_checkpoint_then_retry(self):
        p=batch.prepare(self.f.path,self.documents());cancel=threading.Event();cancel.set()
        with self.assertRaises(Cancelled):lc.commit(self.f.path,p,cancel=cancel)
        self.assertEqual(lc.history(self.f.path,self.f.h,'return'),[])
        cancel.clear()
        def progress(message):
            if str(message).startswith('留存原件'):cancel.set()
        with self.assertRaises(Cancelled):lc.commit(self.f.path,p,progress,cancel)
        self.assertEqual(lc.history(self.f.path,self.f.h,'return'),[])
        cancel.clear();lc.commit(self.f.path,p,cancel=cancel);self.assertEqual(self.f.view()['status'],'已收齐')
    def test_actual_headers_auto_detect_and_bad_file_is_explicit(self):
        ds=self.documents();paths=[d['scan']['files'][0]['path'] for d in ds]
        data=batch.read([*paths,paths[0],str(self.f.root/'missing.xlsx')])
        self.assertEqual([d['scan']['stage'] for d in data['documents']],['receipt','payment','return'])
        self.assertEqual(len(data['issues']),1);self.assertIn('missing.xlsx',data['issues'][0]['path'])
        p=batch.prepare(self.f.path,data['documents']);self.assertEqual(len(p['plans']),3)
    def test_file_change_after_preview_rejects_all(self):
        p=batch.prepare(self.f.path,self.documents());source=self.f.root/p['files'][0]['name'];source.write_bytes(source.read_bytes()+b'changed')
        with self.assertRaisesRegex(ValueError,'文件在预览后变化'):lc.commit(self.f.path,p)
        self.assertEqual(lc.history(self.f.path,self.f.h,'return'),[])
    def test_missing_number_requires_binding_unless_explicit_scope(self):
        d=batch.group_scan(self.f.scan(items=[{'补货单号':None}]))[0]
        with self.assertRaisesRegex(ValueError,'缺号'):batch.prepare(self.f.path,[d])
        d['target']=self.f.h;p=batch.prepare(self.f.path,[d],self.f.heads[:1]);self.assertEqual(p['plans'][0]['head']['number'],self.f.h['number'])

    def test_export_partial_failure_keeps_complete_batch_receipt(self):
        with patch('normal_delivery.export_signed',side_effect=[['first.xlsx'],RuntimeError('second failed')]):
            with self.assertRaises(RuntimeError) as error:
                batch.export_signed(self.f.path,self.f.heads,self.f.root)
        self.assertIn('first.xlsx',str(error.exception))
        self.assertIn(self.f.heads[1]['number'],str(error.exception))
        self.assertIn('second failed',str(error.exception))

if __name__=='__main__':unittest.main()
