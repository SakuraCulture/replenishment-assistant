"""Local purchase eligibility precedes browser calls. Synthetic stores/clients only."""
import copy
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from aoxiang_client import ComponentError
from aoxiang_purchase import PaymentPreparationError
from aoxiang_purchase_messages import PAYMENT_PREPARATION_CODES, item_message
from aoxiang_workflow_runner import Runner
from aoxiang_workflow_state import can_end_local, local_preparation
from aoxiang_workflows import Workflows
from deletion_barrier import SourceDeleted
from runtime_support import Cancelled, checkpoint


def head(n):
    return {'oid':str(n), 'day':'2026-11-11', 'store':str(n), 'name':'合成店'+str(n),
            'warehouse':'河北仓', 'number':'SYNTHETIC-'+str(n), 'revision':1,
            'current_revision':1, 'digest':'a'*64, 'visibility_token':'synthetic-visible'}


def payment_error(code='PAYMENT_MISSING'):
    return PaymentPreparationError(code, '合成付款原因：需核对原付款资料', {'synthetic':True, 'payment':None})


class FakeClient:
    def __init__(self):
        self.calls=[]
        self.login_hook=lambda count: None
        self.config={'configured':True, 'enabled':True, 'login_policy':'current_edge',
                     'account_binding':{'account_id':'synthetic-account', 'merchant_code':'synthetic-merchant'},
                     'store_mapping_version':'synthetic-mapping',
                     'targets':[{'buyerWarehouseCode':str(n), 'displayName':'合成店'+str(n)} for n in range(10)]}
        self.receipt={'identity':{'accountId':'synthetic-account', 'merchantCode':'synthetic-merchant'}}

    def request(self, action, **values):
        self.calls.append((action,copy.deepcopy(values)))
        if action=='configuration':return copy.deepcopy(self.config)
        if action=='setup.check_login':
            self.login_hook(sum(a=='setup.check_login' for a,_ in self.calls))
            return {'id':'synthetic-login', 'state':'VERIFIED'}
        if action=='template.start':return {'id':'synthetic-template', 'state':'RUNNING'}
        if action=='template.status':return {'id':'synthetic-template', 'state':'READY', 'receipt':copy.deepcopy(self.receipt)}
        if action=='purchase.reserve':return {'record':{'uploadAttempted':False}}
        if action=='purchase.status':return {'state':'RESERVED', 'uploadAttempted':False}
        raise AssertionError('Unexpected platform action: '+action)

    def count(self, action):return sum(a==action for a,_ in self.calls)


class PreflightRunnerTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(prefix='ax-preflight-runner-')
        self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        self.store=Workflows(self.root/'aoxiang')
        self.client=FakeClient()
        self.cancel=threading.Event()
        self.runner=Runner(self.store,self.client,ROOT/'components/aoxiang',self.root/'synthetic.sqlite3',cancel=self.cancel)
        # Guard coverage is separate from payment eligibility. No test opens a
        # production database, attaches a browser, or dispatches a platform call.
        for name,kwargs in [
            ('aoxiang_order_guard.deleted_orders',{'return_value':{}}),
            ('aoxiang_order_guard.require_orders',{'return_value':None}),
            ('aoxiang_workflow_runner.Directory',{}),
            ('aoxiang_workflow_runner.atomic_json',{}),
            ('deletion_barrier.package_finished',{}),
        ]:
            mocked=patch(name,**kwargs);value=mocked.start();self.addCleanup(mocked.stop)
            if name.endswith('Directory'):value.return_value.suppliers.return_value={'河北仓':'synthetic-supplier'}

    def package(self, path, h, configuration, suppliers, receipt, directory, contract, **kw):
        folder=self.root/('package-'+h['oid'])
        return {'id':'synthetic-purchase-'+h['oid'], 'package_file':str(folder/'intent.json'),
                'package_sha256':'b'*64, 'plan_file':str(folder/'basis.json'), 'plan_sha256':'c'*64,
                'intent':{'totalQuantity':'2', 'totalAmount':'1.00', 'rows':[{'sku':'synthetic'}],
                          'supplierCode':'synthetic-supplier'}}

    def test_eight_ineligible_items_make_no_configuration_identity_template_or_purchase_requests(self):
        job=self.store.prepare([head(n) for n in range(8)])
        codes=sorted(PAYMENT_PREPARATION_CODES)*2
        with patch('aoxiang_workflow_runner.preflight_payment',side_effect=[payment_error(c) for c in codes]) as preflight, \
             patch('aoxiang_workflow_runner.prepare',side_effect=AssertionError('must not prepare')):
            out=self.runner.run(job['id'])
        self.assertEqual(preflight.call_count,8)
        self.assertEqual(self.client.calls,[])
        self.assertEqual(out['state'],'COMPLETED_WITH_ERRORS')
        for item,code in zip(out['items'],codes):
            self.assertEqual(item['state'],'REVIEW')
            self.assertEqual(item['error']['code'],code)
            self.assertEqual(item['error']['details'],{'synthetic':True,'payment':None})
            self.assertTrue(local_preparation(item,'prepare'))
            self.assertTrue(can_end_local(out,item))
        self.assertFalse(self.runner.business_path.exists())

    def test_mixed_batch_only_eligible_items_observe_current_login_and_prepare(self):
        job=self.store.prepare([head(n) for n in range(4)])
        with patch('aoxiang_workflow_runner.preflight_payment',side_effect=[payment_error(),{},payment_error('PAYMENT_PENDING'),{}]), \
             patch('aoxiang_workflow_runner.prepare',side_effect=self.package) as prepare:
            out=self.runner.run(job['id'])
        self.assertEqual([i['state'] for i in out['items']],['REVIEW','PREPARED','REVIEW','PREPARED'])
        self.assertEqual([c.args[1]['oid'] for c in prepare.call_args_list],['1','3'])
        self.assertEqual(self.client.count('setup.check_login'),2)
        self.assertEqual(self.client.count('configuration'),4)
        self.assertEqual(self.client.count('template.start'),1)
        self.assertEqual(self.client.count('purchase.reserve'),2)
        self.assertEqual(self.client.count('purchase.start'),0)

    def test_second_eligible_item_still_checks_identity_and_stops_on_account_change(self):
        job=self.store.prepare([head(1),head(2)])
        def changed(count):
            if count==2:raise ComponentError('WRONG_IDENTITY','合成：当前商家已切换')
        self.client.login_hook=changed
        with patch('aoxiang_workflow_runner.preflight_payment',return_value={}), \
             patch('aoxiang_workflow_runner.prepare',side_effect=self.package):
            with self.assertRaises(ComponentError) as caught:self.runner.run(job['id'])
        self.assertEqual(caught.exception.code,'WRONG_IDENTITY')
        out=self.store.get(job['id'])
        self.assertEqual([i['state'] for i in out['items']],['PREPARED','PENDING'])
        self.assertEqual(self.client.count('setup.check_login'),2)
        self.assertEqual(self.client.count('purchase.reserve'),1)

    def test_payment_change_after_preflight_is_rechecked_by_real_freeze_before_reservation(self):
        # Use a real isolated lifecycle DB for the race between the local
        # precheck and final freeze; the browser transport remains FakeClient.
        import test_lifecycle
        fixture=test_lifecycle.LifecycleTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        fixture.save('return');fixture.save('payment',[{'付款单商品数量':7,'实际金额':.99}])
        self.runner.business_path=fixture.path
        self.client.config['targets']=[{'buyerWarehouseCode':fixture.h['store'],'displayName':'合成店'}]
        self.client.login_hook=lambda count:fixture.save('return',[{'门店确认数量':3}])
        job=self.store.prepare([fixture.h])
        out=self.runner.run(job['id'])
        self.assertEqual(out['items'][0]['state'],'REVIEW')
        self.assertIn(out['items'][0]['error']['code'],PAYMENT_PREPARATION_CODES)
        self.assertEqual(self.client.count('setup.check_login'),1)
        self.assertEqual(self.client.count('purchase.reserve'),0)

    def test_upload_still_validates_frozen_basis_immediately_before_dispatch(self):
        job=self.store.prepare([head(1)])
        package={'id':'synthetic-existing'}
        job=self.store.update(job['id'],state='READY',position=0,changes={'state':'PREPARED','package':package})
        upload=self.store.upload(job['id'],[0],job['sequence'])
        with patch('aoxiang_workflow_runner.preflight_payment',side_effect=AssertionError('existing upload is not a new preview')), \
             patch('aoxiang_workflow_runner.validate_for_upload',side_effect=ValueError('付款单已更新')) as validate:
            out=self.runner.run(upload['id'])
        self.assertEqual(validate.call_count,1)
        self.assertEqual(out['items'][0]['state'],'REVIEW')
        self.assertEqual(self.client.count('purchase.start'),0)

    def test_cancel_during_preflight_keeps_pending_and_dispatches_nothing(self):
        job=self.store.prepare([head(1),head(2)])
        def stop(*args,**kwargs):self.cancel.set();checkpoint(self.cancel)
        with patch('aoxiang_workflow_runner.preflight_payment',side_effect=stop):
            with self.assertRaises(Cancelled):self.runner.run(job['id'])
        self.assertEqual([i['state'] for i in self.store.get(job['id'])['items']],['PENDING','PENDING'])
        self.assertEqual(self.client.calls,[])

    def test_late_preflight_error_never_overwrites_surviving_item_after_deletion(self):
        job=self.store.prepare([head(1),head(2)])
        original_second=job['items'][1]['item_id']
        def remove_first(path,h,**kwargs):
            if h['oid']=='1':
                with self.store.db(True) as db:
                    current=self.store._get(db,job['id'])
                    current['items']=current['items'][1:];current['items'][0]['position']=0
                    self.store._save(db,current,'SYNTHETIC_CONCURRENT_SOURCE_DELETION')
                raise payment_error()
            return {}
        with patch('aoxiang_workflow_runner.preflight_payment',side_effect=remove_first), \
             patch('aoxiang_workflow_runner.prepare',side_effect=self.package):
            out=self.runner.run(job['id'])
        self.assertEqual(len(out['items']),1)
        self.assertEqual(out['items'][0]['item_id'],original_second)
        self.assertEqual(out['items'][0]['head']['oid'],'2')
        self.assertEqual(out['items'][0]['state'],'PREPARED')
        self.assertIsNone(out['items'][0]['error'])
        self.assertEqual(self.client.count('setup.check_login'),1)

    def test_deleted_source_exception_during_preflight_dispatches_nothing(self):
        job=self.store.prepare([head(1)])
        with patch('aoxiang_workflow_runner.preflight_payment',side_effect=SourceDeleted('合成：订单已删除')):
            out=self.runner.run(job['id'])
        self.assertEqual(out['items'][0]['state'],'PENDING')
        self.assertEqual(self.client.calls,[])

    def test_existing_package_recovery_keeps_original_path_without_new_preflight_or_login(self):
        job=self.store.prepare([head(1)])
        package=self.package(None,head(1),None,None,None,None,None)
        job=self.store.update(job['id'],position=0,changes={'package':{k:package[k] for k in ('id','package_file','package_sha256','plan_file','plan_sha256')}})
        with patch('aoxiang_workflow_runner.preflight_payment',side_effect=AssertionError('old package must keep recovery path')) as preflight:
            out=self.runner.run(job['id'])
        preflight.assert_not_called()
        self.assertEqual(out['items'][0]['state'],'PREPARED')
        self.assertEqual(self.client.count('configuration'),1)
        self.assertEqual(self.client.count('setup.check_login'),0)
        self.assertEqual(self.client.count('template.start'),0)
        self.assertEqual(self.client.count('purchase.reserve'),1)

    def test_unexpected_local_validation_error_is_saved_per_item_and_batch_finishes(self):
        job=self.store.prepare([head(1),head(2)])
        with patch('aoxiang_workflow_runner.preflight_payment',side_effect=ValueError('合成：来源依据异常')):
            out=self.runner.run(job['id'])
        self.assertEqual(out['state'],'COMPLETED_WITH_ERRORS')
        self.assertEqual([i['error']['code'] for i in out['items']],['BUSINESS_REVIEW']*2)
        self.assertEqual(self.client.calls,[])


class LocalErrorPresentationTests(unittest.TestCase):
    def test_new_codes_and_historical_generic_errors_remain_local_and_endable(self):
        for code in [*sorted(PAYMENT_PREPARATION_CODES),'BUSINESS_REVIEW']:
            with self.subTest(code=code):
                item={'state':'REVIEW','error':{'code':code,'message':'原始原因含单号及付款V1到V2变化'}}
                self.assertTrue(local_preparation(item,'prepare'))
                self.assertTrue(can_end_local({'kind':'prepare','state':'COMPLETED_WITH_ERRORS'},item))
                self.assertIn('付款V1到V2',item_message(item,'prepare'))
                for evidence in ({'package':{'id':'old'}},{'purchase_id':'p'},{'uploadAttempted':True},{'purchase_observation':{'taskId':'t'}}):
                    self.assertFalse(local_preparation({**item,**evidence},'prepare'))

    def test_reason_dialog_states_no_new_platform_task_and_keeps_historical_error(self):
        from aoxiang_workflow_ui import QueueDialog
        for code in [*sorted(PAYMENT_PREPARATION_CODES),'BUSINESS_REVIEW']:
            with self.subTest(code=code):
                item={'state':'REVIEW','error':{'code':code,'message':'合成：付款依据已变化'}}
                host=SimpleNamespace(current={'kind':'prepare','items':[item]},table=SimpleNamespace(currentRow=lambda:0))
                with patch('aoxiang_workflow_ui.QMessageBox.information') as show:
                    QueueDialog.failure_reason(host)
                message=show.call_args.args[-1]
                self.assertIn('付款依据已变化',message)
                self.assertIn('未建立新的平台采购任务，未进入上传',message)
                self.assertIn('可结束本地准备',message)
                self.assertIn('不必为删除订单而确认付款',message)
                self.assertEqual(show.call_args.args[1],'任务原因' if code=='BUSINESS_REVIEW' else '本地付款检查未通过')


if __name__=='__main__':unittest.main()
