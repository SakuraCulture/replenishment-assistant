"""Read optimization boundaries: same checked facts, corruption and concurrency."""
import copy,json,sqlite3,threading,unittest,zlib
from contextlib import closing
from unittest.mock import patch
import test_lifecycle as fixture
import test_jst_payment as payment_fixture
import order_lifecycle as lc,jst_orders as jo,work_repository as wr
import aoxiang_purchase as ax
from test_aoxiang_purchase import config
from runtime_support import Cancelled


class PaymentViews(unittest.TestCase):
    def setUp(self):
        self.f=fixture.LifecycleTests();self.f.setUp()
        self.suppliers={'河北仓':'2078246','山东仓':'ZXC2025'}
    def tearDown(self):self.f.tearDown()
    def paid(self,operations=False):
        self.f.save('return')
        if operations:self.f.save('operations')
        self.f.save('payment',[{'付款单商品数量':7,'实际金额':.99}])
    def test_light_view_keeps_complete_payment_health_and_legacy_metadata(self):
        for stage in ('return','operations','payment','receipt'):
            self.f.save(stage)
            full=lc.snapshot(self.f.path,self.f.h);light=lc.snapshot(self.f.path,self.f.h,_comparison=False)
            self.assertEqual(light['rows'],[])
            for field in ('head','health','legacy_return','present','status','next','note','flow_version'):
                self.assertEqual(light[field],full[field],field)
            self.assertEqual(light['documents']['payment'],full['documents']['payment'])
            for name,document in full['documents'].items():
                self.assertEqual(lc.document_summary(light['documents'][name]),lc.document_summary(document))
    def test_freeze_and_validation_exact_against_full_view(self):
        self.paid(True);self.f.save('receipt')
        actual=ax.freeze_payment(self.f.path,self.f.h,config(),self.suppliers)
        snapshot=lc.snapshot
        def full(*args,**kwargs):kwargs.pop('_comparison',None);return snapshot(*args,**kwargs)
        with patch('order_lifecycle.snapshot',side_effect=full):expected=ax.freeze_payment(self.f.path,self.f.h,config(),self.suppliers)
        self.assertEqual(wr.dumps(actual),wr.dumps(expected));self.assertTrue(ax.validate_basis(self.f.path,actual,config(),self.suppliers))
        self.assertEqual(actual['rows'][0]['unit_price'],'0.14');self.assertEqual(actual['rows'][0]['payment_amount'],'0.99')
    def test_payment_owned_snapshot_does_not_alias_inputs_db_or_other_preview(self):
        self.paid();h=copy.deepcopy(self.f.h);c=config();before=copy.deepcopy(c)
        first=ax.freeze_payment(self.f.path,h,c,self.suppliers);second=ax.freeze_payment(self.f.path,h,c,self.suppliers)
        stored=self.f.view()['documents']['payment'];first['payment']['payload']['rows'][0]['base_quantity']='888'
        first['rows'][0]['sources'].append({'private':'edit'})
        self.assertEqual(self.f.view()['documents']['payment'],stored);self.assertEqual(second['payment'],stored)
        self.assertEqual(h,self.f.h);self.assertEqual(c,before)
        self.assertNotEqual(first['payment'],second['payment'])
    def test_every_lifecycle_stage_still_fully_verifies_raw_bytes(self):
        self.paid(True);self.f.save('receipt')
        for stage in ('return','operations','payment','receipt'):
            with self.subTest(stage=stage):
                with closing(sqlite3.connect(self.f.path)) as db,db:
                    old=db.execute('SELECT payload FROM lifecycle_versions WHERE stage=?',(stage,)).fetchone()[0]
                    bad=json.loads(old);bad['rows'][0]['base_quantity']='999'
                    db.execute('UPDATE lifecycle_versions SET payload=? WHERE stage=?',(wr.dumps(bad),stage))
                with self.assertRaisesRegex(ValueError,'快照校验失败'):ax.freeze_payment(self.f.path,self.f.h,config(),self.suppliers)
                with closing(sqlite3.connect(self.f.path)) as db,db:db.execute('UPDATE lifecycle_versions SET payload=? WHERE stage=?',(old,stage))
    def test_signed_chunk_corruption_still_rejected(self):
        self.paid()
        with closing(sqlite3.connect(self.f.path)) as db,db:
            db.execute('UPDATE snapshot_chunks SET data=?',(b'broken',))
        with self.assertRaises((ValueError,zlib.error)):ax.freeze_payment(self.f.path,self.f.h,config(),self.suppliers)
    def test_stale_upstream_pending_and_real_currency_keep_original_errors(self):
        self.paid(True);plan=ax.freeze_payment(self.f.path,self.f.h,config(),self.suppliers)
        self.f.save('return',[{'门店确认数量':3}])
        with self.assertRaisesRegex(ValueError,'对照依据变化'):ax.validate_basis(self.f.path,plan,config(),self.suppliers)
        with self.assertRaises(ax.PaymentPreparationError):ax.freeze_payment(self.f.path,self.f.h,config(),self.suppliers)
    def test_cancel_still_prevents_local_purchase_plan(self):
        self.paid();cancel=threading.Event();cancel.set()
        with self.assertRaises(Cancelled):ax.freeze_payment(self.f.path,self.f.h,config(),self.suppliers,cancel=cancel)


class NaturalPayment(unittest.TestCase):
    def setUp(self):self.f=payment_fixture.DirectPayment();self.f.setUp();self.path=self.f.path;self.gid=self.f.gids[0]
    def tearDown(self):self.f.tearDown()
    def test_full_document_decode_deduplicated_but_exact_plan_retained(self):
        jo.payment_plan(self.path,self.gid)
        with patch('lifecycle_facts.decode',wraps=lc.decode) as decode:actual=jo.payment_plan(self.path,self.gid)
        self.assertEqual(sum(c.args[0] is not None for c in decode.call_args_list),1)
        make=lc.make_plan
        def without(*args,**kwargs):kwargs.pop('_verified',None);return make(*args,**kwargs)
        with patch('order_lifecycle.make_plan',side_effect=without):expected=jo.payment_plan(self.path,self.gid)
        self.assertEqual(wr.dumps(actual),wr.dumps(expected))
        receipt=lc.commit(self.path,actual);self.assertTrue(receipt[0]['changed'])
        with patch('lifecycle_facts.decode',wraps=lc.decode) as decode:repeat=jo.payment_plan(self.path,self.gid)
        self.assertEqual(sum(c.args[0] is not None for c in decode.call_args_list),2)
        self.assertFalse(lc.commit(self.path,repeat)[0]['changed'])
    def test_upstream_bytes_change_between_reads_cannot_reuse_evidence(self):
        original=lc.make_plan;head=jo.get(self.path,self.gid)['head']
        def changed(*args,**kwargs):
            with closing(sqlite3.connect(self.path)) as db,db:
                raw=db.execute("SELECT payload FROM lifecycle_versions WHERE stage='return' AND oid=? AND revision=? AND warehouse=?",lc.key(head)).fetchone()[0]
                p=wr.loads(raw);p['rows'][0]['base_quantity']='987'
                db.execute("UPDATE lifecycle_versions SET payload=? WHERE stage='return' AND payload=?",(wr.dumps(p),raw))
            return original(*args,**kwargs)
        with patch('order_lifecycle.make_plan',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'快照校验失败'):jo.payment_plan(self.path,self.gid)
    def test_upstream_change_after_review_still_blocks_commit_and_keeps_stock(self):
        plan=jo.payment_plan(self.path,self.gid);before=self.f.stock()
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute("UPDATE lifecycle_versions SET digest='changed' WHERE stage='return'")
        with self.assertRaisesRegex(ValueError,'快照校验失败'):lc.commit(self.path,plan)
        self.assertEqual(self.f.stock(),before)

if __name__=='__main__':unittest.main()
