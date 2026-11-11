"""Current Edge login is observed before new work; no platform write/replay."""
import copy
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from aoxiang_current_login import current_configuration,current_template
from aoxiang_client import ComponentError
from runtime_support import Cancelled
from aoxiang_workflow_runner import Runner
from aoxiang_acquisition import prepare


class Client:
    def __init__(self,changed=None,states=None):
        self.before={'configured':True,'enabled':True,'login_policy':'current_edge',
                     'account_binding':{'account_id':'old','merchant_code':'M'},'targets':[{'buyerWarehouseCode':'S'}]}
        self.after=changed or {**self.before,'account_binding':{'account_id':'current','merchant_code':'M'}}
        self.calls=[];self.states=states or [{'id':'check','state':'VERIFIED'}];self.started=False
    def request(self,action,**values):
        self.calls.append((action,values))
        if action=='configuration':return copy.deepcopy(self.after if self.started else self.before)
        if action=='setup.check_login':self.started=True;return self.states.pop(0)
        if action=='setup.status':return self.states.pop(0)
        if action=='setup.cancel':return {'state':'CANCELLED'}
        raise AssertionError('Unexpected action '+action)


class CurrentLoginTests(unittest.TestCase):
    def test_new_work_receives_actual_observed_account_without_expected_identity(self):
        c=Client(states=[{'id':'check','state':'RUNNING'},{'id':'check','state':'VERIFIED'}])
        with patch('aoxiang_current_login.time.sleep'):
            result=current_configuration(c)
        self.assertEqual(result['account_binding']['account_id'],'current')
        self.assertEqual(c.calls,[('configuration',{}),('setup.check_login',{}),('setup.status',{'setup_id':'check'}),('configuration',{})])

    def test_other_merchant_is_connected_but_old_store_mappings_are_not_used(self):
        c=Client(changed={'configured':True,'enabled':False,'login_policy':'current_edge',
                         'needs_store_verification':True,'display_name':'另一商家','targets':[]})
        with self.assertRaisesRegex(ComponentError,'核验该商家的门店') as caught:current_configuration(c)
        self.assertEqual(caught.exception.code,'TARGET_UNVERIFIED')
        self.assertFalse(any(action.startswith(('reserve','purchase.')) for action,_ in c.calls))

    def test_failed_identity_read_does_not_fall_back_to_saved_success(self):
        c=Client(states=[{'id':'check','state':'FAILED','error':{'code':'WAIT_LOGIN','message':'请登录'}}])
        with self.assertRaisesRegex(ComponentError,'请登录'):current_configuration(c)
        self.assertEqual([x[0] for x in c.calls],['configuration','setup.check_login'])

    def test_failure_keeps_phase_and_diagnostics_and_progress_identifies_new_probe(self):
        details={'phase':'等待身份请求','timeout_ms':30000,'diagnostic':'offline timeout'}
        c=Client(states=[{'id':'check','state':'RUNNING','stage':'正在刷新页面'},
                         {'id':'check','state':'FAILED','error':{'code':'IDENTITY_RESPONSE_TIMEOUT','message':'本次核验未完成','details':details}}])
        progress=[]
        with patch('aoxiang_current_login.time.sleep'),self.assertRaises(ComponentError) as caught:current_configuration(c,progress.append)
        self.assertEqual(caught.exception.details,details);self.assertIn('等待身份请求',str(caught.exception))
        self.assertTrue(any('正在刷新页面' in p and '尚未开始本次取数' in p and '秒' in p for p in progress))
        self.assertFalse(any(action in {'reserve','resume'} for action,_ in c.calls))

    def test_cancelled_probe_is_settled_without_creating_business_tasks(self):
        cancel=threading.Event();c=Client(states=[{'id':'check','state':'RUNNING'},{'id':'check','state':'CANCELLED'}])
        def progress(message):cancel.set()
        with patch('aoxiang_current_login.time.sleep'),self.assertRaises(Cancelled):current_configuration(c,progress,cancel)
        self.assertEqual([x[0] for x in c.calls],['configuration','setup.check_login','setup.cancel','setup.status'])
        c=Client()
        with self.assertRaises(Cancelled):current_configuration(c,cancel=cancel)
        self.assertEqual(c.calls,[])

    def test_unconfigured_or_older_transport_remains_metadata_only(self):
        for config in [{'configured':False},{'configured':True}]:
            c=Mock();c.request.return_value=config
            self.assertEqual(current_configuration(c),config);c.request.assert_called_once_with('configuration')

    def test_new_purchase_does_not_reuse_template_from_previous_account(self):
        store=Mock();client=Mock();cancel=threading.Event()
        runner=Runner(store,client,'.','isolated.sqlite3',cancel=cancel)
        old={'identity':{'accountId':'old','merchantCode':'M'},'attemptId':'old-template'}
        new={'identity':{'accountId':'current','merchantCode':'M'},'attemptId':'new-template'}
        job={'id':'job','template_receipt':old,'execution_token':'token'}
        config={'account_binding':{'account_id':'current','merchant_code':'M'}}
        client.request.side_effect=[{'id':'t'},{'state':'READY','receipt':new}]
        self.assertEqual(runner.template(job,config),new)
        self.assertEqual([c.args[0] for c in client.request.call_args_list],['template.start','template.status'])
        store.update.assert_called_once_with('job',execution_token='token',template_receipt=new)
        client.reset_mock();job['template_receipt']=new
        self.assertEqual(runner.template(job,config),new);client.request.assert_not_called()

    def test_account_switch_does_not_allow_duplicate_unfinished_store_export(self):
        c=Client();config={**c.after,'store_mapping_version':'v'}
        pending={'id':'original','command':{'operation':'replenishment.export',
                 'account_binding':c.before['account_binding'],'scope':{'warehouse_codes':['S']}}}
        jobs=Mock();jobs.unfinished.return_value=[pending]
        with self.assertRaisesRegex(ValueError,'原采集'):prepare(jobs,config,['S'],'refresh',day='2026-11-10')
        jobs.register.assert_not_called()
        config['account_binding']={'account_id':'different','merchant_code':'different-merchant'}
        prepare(jobs,config,['S'],'refresh',day='2026-11-10');jobs.register.assert_called_once()

    def test_single_purchase_refreshes_old_template_without_replaying_a_purchase(self):
        c=Mock();old={'identity':{'accountId':'old','merchantCode':'M'}}
        new={'identity':{'accountId':'current','merchantCode':'M'}}
        config={'account_binding':{'account_id':'current','merchant_code':'M'}}
        c.request.side_effect=[{'id':'t','state':'RUNNING'},{'id':'t','state':'READY','receipt':new}]
        with patch('aoxiang_current_login.time.sleep'):self.assertEqual(current_template(c,old,config),new)
        self.assertEqual([x.args[0] for x in c.request.call_args_list],['template.start','template.status'])
        c.reset_mock();self.assertEqual(current_template(c,new,config),new);c.request.assert_not_called()


if __name__=='__main__':unittest.main()
