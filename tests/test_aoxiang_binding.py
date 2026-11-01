"""Existing-purchase recovery tests; no real AX connection or external writes."""
import copy
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src')]
from aoxiang_client import ComponentError
import test_aoxiang_purchase_ui as ui_fixture


class Client(ui_fixture.Client):
    def __init__(self,*args):
        super().__init__(*args)
        self.binding={'purchase_id':'p1','sequence':5,'active':False,'proposal':{
            'id':'bind-1','state':'READY','request':{'order_id':'PO1','created_day':'2026-11-01','reason':'核对已有单'},
            'summary':{'status':'待提交','rows':1,'quantity':'7','amount':'0.99'},'manifest':{'file':'proof.json','sha256':'a'*64}}}
    def request(self,action,**kw):
        if action.startswith('binding.'):
            self.calls.append((action,copy.deepcopy(kw)))
            if action=='binding.start':self.binding.update(active=True);self.binding['proposal']['state']='RUNNING'
            if action=='binding.pause':self.binding.update(active=False);self.binding['proposal']['state']='PAUSED'
            if action=='binding.confirm':
                self.binding['proposal']['state']='CONFIRMED'
                return ui_fixture.record(state='BOUND',taskId=None,sequence=6,binding={'orderId':'PO1','status':'已完成'},bindingProposal=copy.deepcopy(self.binding['proposal']))
            return copy.deepcopy(self.binding)
        return super().request(action,**kw)


class BindingUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):ui_fixture.PurchaseUiTests.setUpClass()
    def setUp(self):
        self.f=ui_fixture.PurchaseUiTests();self.f.setUp();self.d=self.f.d
        self.d.client=Client();self.d.client.running=True
        self.d.client.records=[ui_fixture.record(state='NEEDS_REVIEW',taskId=None)]
        self.d.load();self.f.wait(lambda:self.d.worker is None)
        self.d.package={'id':'p1','intent':{'target':{'displayName':'测试店'},'supplierCode':'2078246','totalQuantity':'7','totalAmount':'0.99','rows':[{'sku':'0001','name':'合成商品','quantity':'7','amount':'0.99'}]}}
        self.d.render()
    def tearDown(self):self.f.tearDown()

    def test_open_stays_local_unknown_upload_cannot_resend_and_explicit_check_is_separate(self):
        d=self.d
        self.assertTrue(d.binding_start_btn.isEnabled());self.assertFalse(d.upload_btn.isEnabled());self.assertFalse(d.binding_confirm_btn.isEnabled())
        self.assertFalse(any(a=='binding.start' for a,_ in d.client.calls))
        with patch('aoxiang_binding_ui.validate_for_upload',return_value=True):
            d.start_binding_request({'order_id':'PO1','created_day':'2026-11-01','reason':'核对已有单'})
            self.f.wait(lambda:d.worker is None)
        self.assertTrue(d.stop_btn.isEnabled());self.assertFalse(d.binding_confirm_btn.isEnabled())
        d.stop();self.f.wait(lambda:d.worker is None)
        self.assertEqual(d.binding_record['proposal']['state'],'PAUSED')
        self.assertFalse(any(a=='purchase.start' for a,_ in d.client.calls))

    def test_confirmation_defaults_no_and_binds_preview_before_modal_refresh(self):
        from PySide6.QtWidgets import QMessageBox
        d=self.d;d.binding_status(copy.deepcopy(d.client.binding))
        with patch('aoxiang_binding_ui.QMessageBox.question',return_value=QMessageBox.StandardButton.No) as ask:d.binding_confirm()
        self.assertEqual(ask.call_args.args[-1],QMessageBox.StandardButton.No)
        self.assertFalse(any(a=='binding.confirm' for a,_ in d.client.calls))
        def refreshed(*args):
            newer=copy.deepcopy(d.client.binding);newer['sequence']=7;newer['proposal']['id']='bind-2';d.binding_status(newer)
            return QMessageBox.StandardButton.Yes
        with patch('aoxiang_binding_ui.QMessageBox.question',side_effect=refreshed),patch('aoxiang_binding_ui.validate_for_upload',return_value=True):
            d.binding_confirm();self.f.wait(lambda:d.worker is None)
        submitted=[p for a,p in d.client.calls if a=='binding.confirm']
        self.assertEqual(submitted,[{'purchase_id':'p1','proposal_id':'bind-1','sequence':5,'manifest_sha256':'a'*64}])
        self.assertIn('已完成',d.note.text());self.assertFalse(d.binding_confirm_btn.isEnabled())

    def test_stale_other_order_callback_ignored_and_switch_clears_old_preview(self):
        d=self.d;d.binding_status(copy.deepcopy(d.client.binding))
        d.binding_status({**d.client.binding,'purchase_id':'wrong','sequence':100})
        d.binding_status({**d.client.binding,'sequence':1,'proposal':None})
        self.assertEqual(d.binding_record['sequence'],5)
        d.records=[ui_fixture.record(id='second',state='RESERVED',uploadAttempted=False,taskId=None)]
        d.select_record(0);d.update_buttons()
        self.assertIsNone(d.binding_record);self.assertFalse(d.binding_confirm_btn.isEnabled())

    def test_known_import_task_conflict_and_missing_package_block_manual_confirmation(self):
        d=self.d;d.current=ui_fixture.record();d.binding_status(copy.deepcopy(d.client.binding));d.update_buttons()
        self.assertFalse(d.binding_start_btn.isEnabled());self.assertFalse(d.binding_confirm_btn.isEnabled())
        d.current=ui_fixture.record(state='NEEDS_REVIEW',taskId=None,additionalTaskIds=['second']);d.update_buttons()
        self.assertFalse(d.binding_start_btn.isEnabled())
        d.current=ui_fixture.record(state='NEEDS_REVIEW',taskId=None);d.package=None;d.update_buttons()
        self.assertFalse(d.binding_details_btn.isEnabled());self.assertFalse(d.binding_confirm_btn.isEnabled())

    def test_late_receipt_supersedes_preview_and_requires_original_record_refresh(self):
        d=self.d;result=copy.deepcopy(d.client.binding)
        result['proposal']['state']='SUPERSEDED';d.binding_status(result)
        self.assertFalse(d.binding_start_btn.isEnabled());self.assertFalse(d.binding_confirm_btn.isEnabled())
        self.assertIn('刷新本单记录',d.binding_note.text())
        d.purchase_status(ui_fixture.record(state='ACCEPTED',taskId='original',bindingProposal=result['proposal']))
        self.assertTrue(d.verify_btn.isEnabled());self.assertFalse(d.upload_btn.isEnabled())

    def test_real_payment_change_blocks_binding_dispatch_without_changing_original_documents(self):
        from test_aoxiang_purchase_tasks import PurchaseTaskTests
        f=PurchaseTaskTests();f.setUp()
        try:
            p=f.package();f.f.save('payment',[{'付款单商品数量':8,'实际金额':.99}]);before=f.f.view()
            d=self.d;d.package=p;d.host.work_repository.path=f.f.path
            original=d.client.request
            def request(action,**kw):return f.config if action=='configuration' else original(action,**kw)
            with patch.object(d.client,'request',side_effect=request):
                d.start_binding_request({'order_id':'PO1','created_day':'2026-11-01','reason':'核对已有单'});self.f.wait(lambda:d.worker is None)
            self.assertIn('付款单已更新',self.f.errors[-1]);self.assertFalse(any(a=='binding.start' for a,_ in d.client.calls));self.assertEqual(f.f.view(),before)
        finally:f.tearDown()


class BindingProtocolTests(unittest.TestCase):
    def test_real_protocol_missing_browser_binding_fails_without_upload_or_business_changes(self):
        from test_aoxiang_purchase_tasks import PurchaseTaskTests
        f=PurchaseTaskTests();f.setUp()
        try:
            p=f.package();f.reserve(p);before=f.f.view()
            self.assertIsNone(f.client.request('binding.status',purchase_id=p['id'])['proposal'])
            with self.assertRaises(ComponentError):f.client.request('binding.start',purchase_id=p['id'],binding_request={'order_id':'PO1','created_day':'2026-11-01','reason':''})
            f.client.request('binding.start',purchase_id=p['id'],binding_request={'order_id':'PO1','created_day':'2026-11-01','reason':'合成协议验证'})
            end=time.monotonic()+5
            while True:
                r=f.client.request('binding.status',purchase_id=p['id'])
                if not r['active']:break
                if time.monotonic()>end:self.fail('local no-binding failure did not settle')
                time.sleep(.02)
            self.assertEqual(r['proposal']['state'],'FAILED');self.assertIsNone(f.client.request('ping')['active_binding_id'])
            old=f.client.request('purchase.status',purchase_id=p['id']);self.assertFalse(old['uploadAttempted']);self.assertIsNone(old['binding'])
            self.assertEqual(f.f.view(),before)
        finally:f.tearDown()


if __name__=='__main__':unittest.main()
