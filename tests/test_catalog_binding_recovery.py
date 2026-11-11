import copy,json,os,sqlite3,sys,threading,unittest,uuid
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import test_catalog_sync_adoption as seed
from catalog_binding_guard import adoption_guard
from catalog_sync_client import Client,ComponentError,components

class BindingRecovery(unittest.TestCase):
    def setUp(self):
        self.seed=seed.Adoption();self.seed.setUp();self.addCleanup(self.seed.tearDown)
        self.ad=self.seed.ad;self.root=self.seed.node;self.batch=self.seed.batch;self.item=self.seed.item
    def managed(self,retired=False,changed=False):
        item=copy.deepcopy(self.item)
        if retired:item['retirement']={'reason':'QA local end'}
        account=copy.deepcopy(self.item['account'])
        if changed:account['expectedCoid']='999'
        data={'schemaVersion':1,'storeId':'isolated','batches':{'batch':{'id':'batch','items':[item]}},'accounts':{'山东仓':account}}
        (self.root/'jobs.json').write_text(json.dumps(data),'utf-8');(self.root/'store.guard.json').write_text(json.dumps({'schemaVersion':1,'storeId':'isolated'}),'utf-8')
        def reader(bid):
            current=json.loads((self.root/'jobs.json').read_text('utf-8'))
            return {'batches':list(current['batches'].values()),'accounts':list(current['accounts'].values())}
        self.ad.state_reader=reader
    def test_cached_old_item_cannot_apply_after_retirement(self):
        self.managed(retired=True)
        with patch.object(self.ad,'_apply') as apply:
            with self.assertRaisesRegex(ValueError,'本地结束'):self.ad.apply(self.batch,self.item)
            apply.assert_not_called()
        self.assertIsNone(self.ad.catalogs.current_digest('山东仓'))
    def test_changed_binding_blocks_cached_receipt_before_writes(self):
        self.managed(changed=True)
        with patch.object(self.ad,'_apply') as apply:
            with self.assertRaisesRegex(ValueError,'绑定已变化'):self.ad.apply(self.batch,self.item)
            apply.assert_not_called()
    def test_live_managed_receipt_keeps_normal_adoption(self):
        self.managed();ack=self.ad.apply(self.batch,self.item);self.assertEqual(ack['warehouse'],'山东仓');self.assertIsNotNone(self.ad.catalogs.current_digest('山东仓'))
    def test_missing_managed_state_does_not_become_empty_safe_state(self):
        (self.root/'store.guard.json').write_text('{}','utf-8')
        with self.assertRaisesRegex(ValueError,'状态缺失'):self.ad.apply(self.batch,self.item)
    def test_managed_receipt_requires_fresh_service_state(self):
        self.managed();self.ad.state_reader=None
        with patch.object(self.ad,'_apply') as apply:
            with self.assertRaisesRegex(ValueError,'服务状态缺失'):self.ad.apply(self.batch,self.item)
            apply.assert_not_called()
    def test_unavailable_service_never_bypasses_identity_check(self):
        self.managed()
        with patch.object(self.ad,'state_reader',side_effect=RuntimeError('service unavailable')),patch.object(self.ad,'_apply') as apply:
            with self.assertRaisesRegex(RuntimeError,'service unavailable'):self.ad.apply(self.batch,self.item)
            apply.assert_not_called()
    @unittest.skipUnless((components()[1]/'runtime/node.exe').exists(),'locked Node unavailable')
    def test_actual_node_reset_and_python_adoption_share_the_commit_gate(self):
        bootstrap=Client(self.root);bootstrap.start();bootstrap.close();data=json.loads((self.root/'jobs.json').read_text('utf-8'))
        data['accounts']={'山东仓':self.item['account']};data['batches']={'batch':{**self.batch,'items':[self.item]}}
        (self.root/'jobs.json').write_text(json.dumps(data),'utf-8');client=Client(self.root)
        try:
            client.start();plan=client.request('accounts.reset_preview',warehouses=['山东仓']);operation=uuid.uuid4().hex
            request={'warehouses':['山东仓'],'expected_fingerprint':plan['fingerprint'],'operation_id':operation,'confirmed':True}
            self.ad.state_reader=lambda bid:client.request('sync.status',batch_id=bid)
            with adoption_guard(self.root,self.batch,self.item,state_reader=self.ad.state_reader):
                with self.assertRaises(ComponentError) as failed:client.request('accounts.reset_bindings',**request)
                self.assertEqual(failed.exception.code,'ADOPTION_BUSY')
            # Actual service RPC works even if host reads of jobs.json fail;
            # the service is its sole writer and owns the current snapshot.
            original_read=Path.read_text
            def read_text(path,*args,**kw):
                if path==self.root/'jobs.json':raise PermissionError('synthetic concurrent replacement')
                return original_read(path,*args,**kw)
            with patch.object(Path,'read_text',read_text):self.ad.apply(self.batch,self.item)
            client.request('accounts.reset_bindings',**request)
            with self.assertRaisesRegex(ValueError,'本地结束'):self.ad.apply(self.batch,self.item)
        finally:self.assertTrue(client.close())
