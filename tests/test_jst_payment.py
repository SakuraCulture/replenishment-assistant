"""Direct payment scope, source equality and atomic rollback using isolated orders."""
import copy,sqlite3,sys,tempfile,threading,unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/p) for p in ('src','tools')]
import jst_qa,jst_orders as orders,jst_results as results,jst_payment as payment
import order_lifecycle as lc,warehouse_catalog,work_repository as wr
from runtime_support import Cancelled


class DirectPayment(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.backup=patch('storage_chunks.backup_before_write');self.backup.start()
        self.path=jst_qa.fixture(self.root/'seed',2);self.heads=lc.current_targets(self.path)
        for wh,c in warehouse_catalog.Repository(self.path.parent).catalogs().items():
            orders.commit_refresh(self.path,orders.prepare_refresh(self.path,wh,catalog=c))
        self.gids=orders.commit(self.path,orders.prepare(self.path,self.heads))['generated']
        self.data=results.read(self.path,self.root/'index.sqlite3',self.heads[0]['day'])
        self.entries=payment.selection(self.data['records'])
    def tearDown(self):self.backup.stop();self.tmp.cleanup()
    def stock(self):
        with closing(sqlite3.connect(self.path)) as db:
            return {t:db.execute('SELECT * FROM '+t).fetchall() for t in ('jst_baselines','jst_warehouses','jst_active','jst_reservations','jst_locks')}
    def unpaid(self):return all(not lc.snapshot(self.path,h)['present']['payment'] for h in self.heads)
    def reallocate_last_warehouse(self):
        heads=[h for h in self.heads if h['warehouse']==self.entries[-1]['head']['warehouse']]
        return orders.commit(self.path,orders.prepare(self.path,heads,correction=True))
    def test_batch_exact_saved_source_no_export_atomic_save_and_idempotent_repeat(self):
        before=self.stock();plan=payment.prepare(self.path,self.entries)
        self.assertEqual(len(plan['plans']),4);self.assertTrue(self.unpaid());self.assertEqual(plan['files'],[])
        for p,r in zip(plan['plans'],self.entries):
            self.assertEqual(p,orders.payment_plan(self.path,r['gid'])['plans'][0])
        receipt=lc.commit(self.path,plan);self.assertEqual(len(receipt),4)
        self.assertTrue(all(r['changed'] for r in receipt));self.assertEqual(self.stock(),before)
        self.assertTrue(all(not r['changed'] for r in lc.commit(self.path,payment.prepare(self.path,self.entries))))
        for h in self.heads:
            doc=lc.snapshot(self.path,h)['documents']['payment']['payload']
            self.assertTrue(doc['jst_generation']);self.assertFalse(doc['rows'][0]['document_number'])
    def test_exact_checked_subset_and_no_selection_never_falls_back_to_all(self):
        picked={results.key(self.entries[1]['head'])}
        selected=payment.selection(self.data['records'],picked)
        self.assertEqual([r['gid'] for r in selected],[self.entries[1]['gid']])
        lc.commit(self.path,payment.prepare(self.path,selected))
        paid=[h for h in self.heads if lc.snapshot(self.path,h)['present']['payment']]
        self.assertEqual([results.key(h) for h in paid],list(picked))
        with self.assertRaisesRegex(ValueError,'勾选'):payment.selection(self.data['records'],set())
        with self.assertRaisesRegex(ValueError,'不在'):payment.selection(self.data['records'],{('gone','山东仓')})
    def test_invalid_member_blocks_whole_scope_with_order_identity(self):
        rows=copy.deepcopy(self.entries);rows[-1].update(state='stale',reason='回传已变化')
        with self.assertRaisesRegex(ValueError,rows[-1]['head']['number']):payment.selection(rows)
        self.assertTrue(self.unpaid())
        with self.assertRaisesRegex(ValueError,'重复'):payment.prepare(self.path,[self.entries[0],self.entries[0]])
    def test_stale_display_generation_cannot_silently_switch_to_new_active(self):
        self.reallocate_last_warehouse()
        with self.assertRaises(wr.Conflict):payment.prepare(self.path,self.entries)
        self.assertTrue(self.unpaid())
    def test_reallocation_after_review_rolls_back_all_payments(self):
        plan=payment.prepare(self.path,self.entries)
        self.reallocate_last_warehouse()
        with self.assertRaises(wr.Conflict):lc.commit(self.path,plan)
        self.assertTrue(self.unpaid())
    def test_payment_modified_after_review_rolls_back_other_orders(self):
        plan=payment.prepare(self.path,self.entries)
        lc.commit(self.path,orders.payment_plan(self.path,self.entries[-1]['gid']))
        with self.assertRaises(wr.Conflict):lc.commit(self.path,plan)
        self.assertEqual(sum(lc.snapshot(self.path,h)['present']['payment'] for h in self.heads),1)
    def test_cancel_during_prepare_and_before_commit_never_writes(self):
        e=threading.Event();e.set()
        with self.assertRaises(Cancelled):payment.prepare(self.path,self.entries,cancel=e)
        plan=payment.prepare(self.path,self.entries)
        with self.assertRaises(Cancelled):lc.commit(self.path,plan,cancel=e)
        self.assertTrue(self.unpaid())
    def test_missing_positive_price_is_not_invented(self):
        original=orders.payment_plan
        def missing(*args,**kwargs):
            p=original(*args,**kwargs);p['plans'][0]['payload']['rows'][0]['price']=None;return p
        with patch('jst_orders.payment_plan',side_effect=missing):
            with self.assertRaisesRegex(ValueError,'单价或金额待核'):payment.prepare(self.path,self.entries)
        self.assertTrue(self.unpaid())


if __name__=='__main__':unittest.main()
