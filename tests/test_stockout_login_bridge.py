"""Exercise current-login calls through the production analysis RPC whitelist."""
import copy
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from aoxiang_client import ComponentError
from aoxiang_current_login import current_configuration
from runtime_support import Cancelled
from stockout_acquisition import Client
from stockout_task import Bridge


class LoginTransport:
    def __init__(self,failed=False):
        self.calls=[];self.cancelled=False;self.failed=failed;self.closed=False
    def request(self,action,**values):
        self.calls.append((action,copy.deepcopy(values)))
        if action=='configuration':
            return {'configured':True,'enabled':True,'login_policy':'current_edge',
                    'account_binding':{'account_id':'current','merchant_code':'M'},'targets':[]}
        if action=='setup.check_login':return {'id':'analysis-login','state':'RUNNING'}
        if action in {'setup.status','setup.cancel'}:
            assert values=={'setup_id':'analysis-login'}
            if action=='setup.cancel':self.cancelled=True
            return {'id':'analysis-login','state':'CANCELLED' if self.cancelled else 'FAILED' if self.failed else 'VERIFIED',
                    'error':{'code':'WAIT_LOGIN','message':'请在Edge登录'} if self.failed else None}
        raise AssertionError('Unexpected platform action: '+action)
    def start(self):pass
    def close(self,timeout=.5):self.closed=True;return True


class StockoutLoginBridgeTests(unittest.TestCase):
    def setUp(self):
        self.cancel=threading.Event();self.transport=LoginTransport();self.leases=[];self.releases=[]
        gate=SimpleNamespace(claim_background=lambda:self.cancel,release_background=self.releases.append)
        def lease(**values):self.leases.append(values);return self.transport
        host=SimpleNamespace(aoxiang_workflows=SimpleNamespace(gate=gate,session=SimpleNamespace(lease=lease)))
        self.bridge=Bridge(host,self.cancel,lambda _:None)
        self.client=Client(lambda service,**values:self.bridge.request({'service':service,**values}),'ax')
    def tearDown(self):self.bridge.close()

    def test_real_bridge_forwards_current_login_and_poll_on_one_shared_lease(self):
        with patch('aoxiang_current_login.time.sleep'):
            config=current_configuration(self.client,cancel=self.cancel)
        self.assertEqual(config['account_binding']['account_id'],'current')
        self.assertEqual([a for a,_ in self.transport.calls],['configuration','setup.check_login','setup.status','configuration'])
        self.assertEqual(len(self.leases),1)
        self.bridge.release_ax();self.assertTrue(self.transport.closed);self.assertEqual(self.releases,[self.cancel])

    def test_real_bridge_can_cancel_its_login_probe_without_export(self):
        def progress(message):self.cancel.set()
        with patch('aoxiang_current_login.time.sleep'),self.assertRaises(Cancelled):
            current_configuration(self.client,progress,self.cancel)
        self.assertEqual([a for a,_ in self.transport.calls],['configuration','setup.check_login','setup.cancel','setup.status'])
        self.assertTrue(self.transport.cancelled)

    def test_login_failure_remains_failure_without_dispatch(self):
        self.transport.failed=True
        with patch('aoxiang_current_login.time.sleep'),self.assertRaises(ComponentError) as caught:
            current_configuration(self.client,cancel=self.cancel)
        self.assertEqual(caught.exception.code,'WAIT_LOGIN')
        self.assertEqual([a for a,_ in self.transport.calls],['configuration','setup.check_login','setup.status'])

    def test_only_the_three_login_actions_are_added_other_business_stays_blocked(self):
        for action in ['setup.start','setup.commit','setup.verify_saved','purchase.reserve','purchase.start','receipt.start','binding.confirm','template.start']:
            with self.subTest(action=action),self.assertRaisesRegex(ValueError,'分析不允许该翱象操作'):
                self.client.request(action)
        self.assertEqual(self.transport.calls,[]);self.assertEqual(self.leases,[])


if __name__=='__main__':unittest.main()
