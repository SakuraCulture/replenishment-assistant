"""Independent operations/return parity and recovery fixtures; no platforms."""
import copy,sqlite3,unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
import openpyxl
import test_lifecycle as fixture
from decimal import Decimal
from test_jst import catalog
import operations_flow as flow
import order_lifecycle as lc,lifecycle_io as io,lifecycle_batch as batch
import jst_orders as j,purchases,work_repository as wr


class Operations(unittest.TestCase):
    def setUp(self):
        self.f=fixture.LifecycleTests();self.f.setUp();self.path=self.f.path;self.h=self.f.h
        self.backup=patch('storage_chunks.backup_before_write');self.backup.start()
    def tearDown(self):self.backup.stop();self.f.tearDown()
    def route(self):
        with closing(sqlite3.connect(self.path)) as db,db:
            flow.schema(db);db.execute('INSERT INTO lifecycle_routes VALUES(?,?,2)',(self.h['oid'],self.h['revision']))
    def stock(self,n=100):j.commit_refresh(self.path,j.prepare_refresh(self.path,'河北仓',catalog=catalog(stock=n)))

    def test_old_paid_received_orders_keep_original_route(self):
        for s in io.STAGES:self.f.save(s)
        v=self.f.view();self.assertEqual(v['status'],'已收齐');self.assertEqual(v['flow_version'],1)
        self.assertNotIn('operations',v['documents'])

    def test_return_waits_only_for_operations_then_adopts_without_rewriting(self):
        self.route();self.f.save('return',[{'门店确认数量':40}]);self.stock()
        with closing(purchases.connect(self.path)) as db:before=lc.current(db,self.h,'return')
        self.assertEqual(self.f.view()['next'],'operations')
        with self.assertRaisesRegex(ValueError,'运营'):j.prepare(self.path,[self.h])
        flow.adopt(self.path,[self.h]);v=self.f.view();self.assertEqual(v['next'],'payment')
        self.assertTrue(v['health']['operations']);self.assertEqual(Decimal(v['documents']['operations']['payload']['rows'][0]['base_quantity']),40)
        with closing(purchases.connect(self.path)) as db:self.assertEqual(lc.current(db,self.h,'return'),before)

    def test_same_quantity_rules_for_return_and_operations(self):
        self.f.save('return',[{'门店确认数量':5}])
        for q in [None,0,2,10]:
            with self.subTest(q=q):
                a=self.f.plan(self.f.scan('return',[{'门店确认数量':q}]))['plans'][0]
                b=self.f.plan(self.f.scan('operations',[{'门店确认数量':q}]))['plans'][0]
                for k in ['base_quantity','quantity_origin','outside_basis']:
                    self.assertEqual(a['payload']['rows'][0][k],b['payload']['rows'][0][k])
                self.assertEqual(a['payload']['missing'],b['payload']['missing'])
        self.assertEqual(io.fields('return'),io.fields('operations'))

    def test_quantity_increases_and_missing_price_do_not_block_operations(self):
        self.f.save('return',[{'门店确认数量':5}])
        self.f.save('operations',[{'门店确认数量':90}])
        v=self.f.view();self.assertTrue(v['health']['operations'])
        self.assertEqual(v['documents']['return']['payload']['rows'][0]['base_quantity'],'5')
        self.assertEqual(Decimal(v['documents']['operations']['payload']['rows'][0]['base_quantity']),90)

    def test_adopt_replay_is_idempotent_and_other_warehouse_unaffected(self):
        self.route();self.f.save('return');one=flow.adopt(self.path,[self.h]);two=flow.adopt(self.path,[self.h])
        self.assertEqual(one[0]['version'],1);self.assertFalse(two[0]['changed']);self.assertEqual(two[0]['version'],1)
        other=next(h for h in self.f.heads if h['warehouse']!=self.h['warehouse'])
        self.assertFalse(self.f.view(other)['present'].get('operations'))

    def test_allocation_uses_operations_instead_of_return(self):
        self.f.save('return',[{'门店确认数量':40}]);self.f.save('operations',[{'门店确认数量':12}]);self.stock(7)
        p=j.prepare(self.path,[self.h]);r=p['payload']['plans'][0]['rows'][0]
        self.assertEqual(r['demand'],'12');self.assertEqual(r['quantity'],'7')
        done=j.commit(self.path,p);self.assertFalse(done['errors'])
        payment=j.payment_plan(self.path,done['generated'][0]);lc.commit(self.path,payment)
        self.assertEqual(self.f.view()['documents']['payment']['payload']['rows'][0]['base_quantity'],'7')

    def test_return_changed_after_adopt_preview_cannot_commit_stale(self):
        self.f.save('return');p=flow.adopt_plan(self.path,[self.h])
        self.f.save('return',[{'门店确认数量':9}])
        with self.assertRaisesRegex(wr.Conflict,'上游'):lc.commit(self.path,p)
        self.assertFalse(self.f.view()['present'].get('operations'))

    def test_operations_change_keeps_paid_fact_and_allows_explicit_reallocation(self):
        self.f.save('return',[{'门店确认数量':40}]);flow.adopt(self.path,[self.h]);self.stock()
        done=j.commit(self.path,j.prepare(self.path,[self.h]));gid=done['generated'][0]
        lc.commit(self.path,j.payment_plan(self.path,gid))
        with closing(purchases.connect(self.path)) as db:before=lc.current(db,self.h,'payment')
        self.f.save('operations',[{'门店确认数量':20}]);self.assertFalse(self.f.view()['health']['payment'])
        with closing(purchases.connect(self.path)) as db:
            self.assertEqual(lc.current(db,self.h,'payment'),before)
            self.assertIsNone(j.allocation_gate(db,self.h,gid,True)[0])
        p=j.prepare(self.path,[self.h],correction=True)
        self.assertEqual(p['payload']['plans'][0]['rows'][0]['demand'],'20')

    def test_export_visible_layout_identical_and_mixed_import_tagged(self):
        self.f.save('return');flow.adopt(self.path,[self.h])
        returns=lc.export(self.path,[self.h],self.f.root,'return')['files'][0]
        ops=lc.export(self.path,[self.h],self.f.root,'operations')['files'][0]
        a=openpyxl.load_workbook(returns);b=openpyxl.load_workbook(ops)
        try:
            self.assertEqual(a.sheetnames,b.sheetnames)
            for x,y in zip(a,b):
                self.assertEqual(list(x.values),list(y.values))
                self.assertEqual([c._style for row in x for c in row],[c._style for row in y for c in row])
        finally:a.close();b.close()
        data=batch.read([ops]);self.assertFalse(data['issues']);self.assertEqual(data['documents'][0]['scan']['stage'],'operations')

    def test_unready_adopt_has_explicit_return_path_and_no_lock(self):
        self.route()
        with self.assertRaisesRegex(ValueError,'先完成回传'):flow.adopt(self.path,[self.h])
        self.f.save('return');flow.adopt(self.path,[self.h]);self.assertTrue(self.f.view()['health']['operations'])

    def test_cancel_does_not_save_or_leave_busy_state(self):
        import threading
        from runtime_support import Cancelled
        self.f.save('return');p=flow.adopt_plan(self.path,[self.h]);c=threading.Event();c.set()
        with self.assertRaises(Cancelled):lc.commit(self.path,p,cancel=c)
        self.assertFalse(self.f.view()['present'].get('operations'))
        flow.adopt(self.path,[self.h]);self.assertTrue(self.f.view()['present']['operations'])

    def test_native_sign_marks_new_route_without_changing_model(self):
        from test_preissue import result
        import normal_orders as no
        from version_info import MODEL_VERSION
        work=wr.Repository(self.f.root/'native');repo=no.Repository(work);r=result()
        payload={'schema':1,'day':wr.today(),'model_version':MODEL_VERSION,'input_dirty':False,'lifecycle_flow':2,
                 'results':{'S1':r},'states':{'S1':{'state':'已完成','name':'合成店'}}}
        repo.confirm(payload,['S1'],None,{'S1':None})
        heads=lc.current_targets(work.path)
        self.assertTrue(heads)
        with closing(sqlite3.connect(work.path)) as db:
            self.assertTrue(all(flow.required(db,h) for h in heads))
        self.assertTrue(all(lc.snapshot(work.path,h)['flow_version']==2 for h in heads))
        self.assertEqual(repo.get(wr.today(),'S1')['payload']['result'],r)

    def test_mixed_four_stage_import_sorts_dependencies_and_saves_atomically(self):
        scans=[self.f.scan(s) for s in ('receipt','payment','operations','return')]
        documents=[{'scan':s,'target':self.h,'selected':True} for s in scans]
        plan=batch.prepare(self.path,documents,[self.h])
        self.assertEqual([p['stage'] for p in plan['plans']],list(io.ALL_STAGES))
        done=lc.commit(self.path,plan);self.assertEqual(len(done),4);self.assertEqual(self.f.view()['status'],'已收齐')
        self.assertTrue(self.f.view()['health']['operations'])

    def test_deletion_after_adopt_preview_does_not_restore_old_order(self):
        import archive_orders
        self.f.save('return');p=flow.adopt_plan(self.path,[self.h]);archive_orders.change(self.path,self.h,True)
        with self.assertRaises(wr.Conflict):lc.commit(self.path,p)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertIsNone(lc.current(db,self.h,'operations'))


if __name__=='__main__':unittest.main()
