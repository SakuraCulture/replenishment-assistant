"""0.22 explicit source selection, immutable stock, durable tasks; synthetic data."""
import sys,sqlite3,threading,copy
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import test_lifecycle as fixture
from test_jst import catalog
import jst_orders as j,jst_exports as ex,jst_tasks as tasks
import lifecycle_io as io,lifecycle_batch as batch,order_lifecycle as lc
import openpyxl
from runtime_support import Cancelled


class PaymentSources(unittest.TestCase):
    def setUp(self):
        self.f=fixture.LifecycleTests();self.f.setUp();self.path=self.f.path;self.h=self.f.h
        self.f.save('return',[{'门店确认数量':48}])
        j.commit_refresh(self.path,j.prepare_refresh(self.path,'河北仓',catalog=catalog(stock=100,step=24,price=2.5)))
    def tearDown(self):self.f.tearDown()
    def generate(self):return j.commit(self.path,j.prepare(self.path,[self.h]))['generated'][0]
    def stock(self):
        with closing(sqlite3.connect(self.path)) as db:
            return {t:db.execute('SELECT * FROM '+t).fetchall() for t in ('jst_baselines','jst_warehouses','jst_active','jst_reservations','jst_locks')}
    def fileplan(self,gid):
        p=ex.export(self.path,[gid],self.f.root/'out')['files'][0]['path']
        read=batch.read([p]);self.assertFalse(read['issues'])
        return p,batch.prepare(self.path,read['documents'])
    def test_four_column_roundtrip_same_business_and_no_fake_external_id(self):
        gid=self.generate();direct=j.payment_plan(self.path,gid);_,file=self.fileplan(gid)
        for a,b in zip(direct['plans'][0]['payload']['rows'],file['plans'][0]['payload']['rows']):
            for field in ('sku','base_quantity','amount','currency','document_number','price','upstream_quantity','difference'):
                self.assertEqual(a.get(field),b.get(field),field)
        self.assertFalse(file['plans'][0]['payload']['rows'][0]['document_number'])
        before=self.stock();lc.commit(self.path,file);self.assertEqual(before,self.stock());self.assertEqual(self.f.view()['next'],'receipt')
        self.assertFalse(lc.commit(self.path,batch.prepare(self.path,batch.read([self.fileplan(gid)[0]])['documents']))[0]['changed'])
    def test_column_order_blank_and_zero(self):
        p=self.f.root/'reordered.xlsx';b=openpyxl.Workbook();s=b.active;s.append(['数量','订单号','单价','商品编码'])
        s.append([None,self.h['number'],None,'0001']);b.save(p);b.close()
        d=batch.read([p]);self.assertFalse(d['issues']);r=batch.prepare(self.path,d['documents'])['plans'][0]['payload']['rows'][0]
        self.assertEqual(r['base_quantity'],'0');self.assertEqual(r['quantity_origin'],'blank')
    def test_positive_missing_price_and_unknown_number_not_guessed(self):
        for price,number in [(None,self.h['number']),(2,'unknown')]:
            p=self.f.root/'invalid.xlsx';b=openpyxl.Workbook();s=b.active;s.append(['商品编码','单价','数量','订单号']);s.append(['0001',price,24,number]);b.save(p);b.close()
            d=batch.read([p])
            if price is None:self.assertTrue(d['issues'])
            else:
                with self.assertRaises(ValueError):batch.prepare(self.path,d['documents'])
    def test_choose_one_source_not_sum_and_calibrate_after_receipt(self):
        gid=self.generate();_,plan=self.fileplan(gid);j.lock(self.path,[gid]);lc.commit(self.path,plan)
        self.f.save('receipt',[{'实际收货数量':24}]);before=self.stock()
        actual=self.f.scan('payment',[{'付款单商品数量':24,'实际金额':60,'付款单号':'JST-123'}])
        docs=batch.group_scan(actual)+batch.group_scan(io.scan('payment',io.payment_specs(io.inspect([self.fileplan(gid)[0]])[0])))
        with self.assertRaisesRegex(ValueError,'多份候选'):batch.prepare(self.path,docs)
        corrected=lc.prepare_numbered(self.path,actual);self.assertTrue(corrected['plans'][0]['calibration']);lc.commit(self.path,corrected)
        self.assertEqual(self.stock(),before);view=self.f.view();self.assertEqual(view['rows'][0][4:6],[24,24]);self.assertEqual(view['next'],'receipt')
        self.assertEqual(len(lc.history(self.path,self.h,'payment')),2)
    def test_task_generation_retry_and_restart_exact_operation(self):
        real=tasks.mark
        def crash(path,task,status,message):
            if status=='done':raise Cancelled('simulate exit after COMMIT')
            return real(path,task,status,message)
        with patch.object(tasks,'mark',crash):
            with self.assertRaises(Cancelled):tasks.drain(self.path,self.f.root)
        before=self.stock();tasks.drain(self.path,self.f.root);self.assertEqual(before,self.stock());self.assertFalse(tasks.pending(self.path))
        self.assertIsNone(self.f.view()['documents']['payment'])
    def test_task_cancel_failure_retry_and_upstream_change(self):
        e=threading.Event();e.set()
        with self.assertRaises(Cancelled):tasks.drain(self.path,self.f.root,cancel=e)
        self.f.save('return',[{'门店确认数量':25}]);r=tasks.drain(self.path,self.f.root);self.assertTrue(r[-1]['error'])
        self.f.save('return',[{'门店确认数量':24}]);tasks.drain(self.path,self.f.root)
        with closing(sqlite3.connect(self.path)) as db:gid=j.active(db,self.h)
        self.assertEqual(j.get(self.path,gid)['rows'][0]['quantity'],'24')
    def test_payment_in_same_commit_skips_automatic_allocation(self):
        self.f.save('payment');before=self.stock();tasks.drain(self.path,self.f.root);self.assertEqual(before,self.stock())
    def test_confirmed_return_tasks_are_atomic_on_cancel(self):
        p=self.f.plan(self.f.scan('return',[{'门店确认数量':72}]));before=tasks.pending(self.path);e=threading.Event();e.set()
        with self.assertRaises(Cancelled):lc.commit(self.path,p,cancel=e)
        self.assertEqual(tasks.pending(self.path),before)

if __name__=='__main__':unittest.main()
