import copy,json,sqlite3,sys,unittest,threading
from contextlib import closing
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src')]
import test_lifecycle as fixtures
from aoxiang_receipt_scope import assess,require,latest,formal_receipt_exists
from aoxiang_workflows import Workflows

class ReceiptScopeTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.LifecycleTests();self.f.setUp();self.f.save('return');self.f.save('payment')
        payment=self.f.view()['documents']['payment'];h=self.f.h
        self.basis={k:h[k] for k in ('number','store','warehouse','oid','revision')}
        self.basis.update(paymentVersion=payment['version'],paymentDigest=payment['digest'])
        self.record={'id':'p1','basis':self.basis,'target':{'displayName':'合成店'},'binding':{'orderId':'PO1','status':'待提交'}}
        self.store=Workflows(self.f.root/'workflows',business_path=self.f.path)
    def tearDown(self):self.f.tearDown()
    def delete(self):
        with closing(sqlite3.connect(self.f.path)) as db,db:db.execute('DELETE FROM normal_orders WHERE oid=?',(self.f.h['oid'],))
    def test_read_only_guard_resolves_node_basis_without_oid_and_rejects_changed_payment(self):
        before=self.f.view();self.assertTrue(assess(self.f.path,[self.record])['p1']['eligible']);self.assertEqual(self.f.view(),before)
        wrong=copy.deepcopy(self.record);wrong['basis']['store']='wrong'
        self.assertFalse(assess(self.f.path,[wrong])['p1']['eligible'])
        self.f.save('payment',[{'付款单商品数量':8,'实际金额':1.23}])
        with self.assertRaisesRegex(ValueError,'付款版本'):require(self.f.path,[{'key':'p1','basis':self.basis}])
    def test_deleted_source_cannot_queue_enable_or_dispatch_due_and_projection_retires_safely(self):
        self.store.configure_schedules([self.record],5,True);job=self.store.receipt([self.record]);self.delete()
        self.assertIsNone(self.store.enqueue_due())
        with self.assertRaises(ValueError):self.store.receipt([self.record])
        with self.assertRaises(ValueError):self.store.configure_schedules([self.record],5,True)
        self.store.retire_receipt_sources();s=self.store.schedule('p1');self.assertTrue(s['retired']);self.assertFalse(s['enabled']);self.assertFalse(s['final_pending'])
        item=self.store.get(job['id'])['items'][0];self.assertEqual(item['state'],'SKIPPED');self.assertEqual(item['order_id'],'PO1')
    def test_void_stops_schedule_without_final_receipt_and_preserves_creation_status(self):
        self.store.configure_schedules([self.record],5,True)
        observation={'state':'VOIDED','terminal':True,'terminalKind':'VOIDED','summary':{'status':'已作废','observedAt':'2026-11-08T00:00:00Z'}}
        self.store.observe_schedule('p1',observation);s=self.store.schedule('p1')
        self.assertFalse(s['enabled']);self.assertFalse(s['final_pending']);self.assertIsNone(self.store.enqueue_due())
        value=latest(self.record,observation);self.assertEqual(value['platform_status'],'已作废');self.assertEqual(self.record['binding']['status'],'待提交')
        self.assertFalse(formal_receipt_exists(self.f.path,self.basis))
    def test_old_void_final_pending_is_retired_without_erasing_platform_identity(self):
        self.store.configure_schedules([self.record],5,True)
        with self.store.db(True) as db:
            import json
            row=json.loads(db.execute('SELECT payload FROM ax_schedules WHERE id=?',('p1',)).fetchone()[0]);row.update(terminal=True,final_pending=True,summary={'status':'已作废'})
            db.execute('UPDATE ax_schedules SET payload=? WHERE id=?',(json.dumps(row),'p1'))
        self.store.retire_receipt_sources();s=self.store.schedule('p1')
        self.assertFalse(s['final_pending']);self.assertFalse(s['enabled']);self.assertEqual(s['order_id'],'PO1')
    def test_legacy_frozen_proof_does_not_attach_recreated_same_number(self):
        from aoxiang_purchase import fingerprint
        plan={'head':self.f.h};record=copy.deepcopy(self.record)
        record['basis']={k:v for k,v in self.basis.items() if k not in ('oid','revision')};record['basis']['planFingerprint']=fingerprint(plan)
        folder=self.f.path.parent/'aoxiang/purchase-packages/p1';folder.mkdir(parents=True);(folder/'付款采购依据.json').write_text(json.dumps(plan),'utf-8')
        self.assertTrue(assess(self.f.path,[record])['p1']['eligible'])
        with closing(sqlite3.connect(self.f.path)) as db,db:db.execute('UPDATE normal_orders SET oid=? WHERE oid=?',('recreated',self.f.h['oid']))
        self.assertFalse(assess(self.f.path,[record])['p1']['eligible'])
    def test_poll_guard_requests_cooperative_pause_after_local_source_deleted(self):
        from types import SimpleNamespace
        from aoxiang_workflow_runner import Runner
        from aoxiang_client import ComponentError
        item={**self.record,'key':'p1','purchase_id':'p1','order_id':'PO1'};calls=[]
        class Client:
            def request(client,action,**kwargs):
                calls.append(action)
                if action=='receipt.status':self.delete();return {'scope':{'orderId':'PO1'},'basis':self.basis,'state':'IDLE'}
                return {}
        runner=SimpleNamespace(business_path=self.f.path,client=Client(),store=self.store,cancel=threading.Event())
        with self.assertRaises(ComponentError):Runner.receipt_item(runner,item,{}, {})
        self.assertEqual(calls,['receipt.status','receipt.pause'])
