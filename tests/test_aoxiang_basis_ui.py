"""Visible source freshness using real frozen payment facts and local-only UI clients."""
import copy
import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import test_aoxiang_purchase_tasks as fixture
from test_aoxiang_purchase_ui import Client as UiClient, record
from aoxiang_directory import atomic_json


class BasisUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.f=fixture.PurchaseTaskTests();self.f.setUp()
        self.package=self.f.package()
        self.f.client.close()
        self.dialogs=[]
        self.host=SimpleNamespace(repository=SimpleNamespace(folder=self.f.f.root),work_repository=SimpleNamespace(path=self.f.f.path))
        atomic_json(self.f.root/'purchase-packages'/self.package['id']/'本地任务索引.json',self.package)
        self.errors=[]
        self.warning=patch('readable_ui.failure',side_effect=lambda *a:self.errors.append(a[-1]));self.warning.start()

    def wait(self,condition):
        from PySide6.QtTest import QTest
        until=time.monotonic()+10
        while not condition() and time.monotonic()<until:
            self.app.processEvents();QTest.qWait(10)
        self.assertTrue(condition())

    def open(self,kind='purchase',state='BOUND'):
        f=self.f;p=self.package
        r=record(id=p['id'],state=state,basis=p['intent']['basis'],uploadAttempted=state=='BOUND',
                 taskId='original-task' if state=='BOUND' else None,binding={'orderId':'original-PO','status':'待提交'} if state=='BOUND' else None)
        class Client(UiClient):
            def __init__(self,*args):super().__init__(*args);self.records=[copy.deepcopy(r)]
            def request(self,action,**kwargs):
                if action=='configuration':return copy.deepcopy(f.config)
                if action=='receipt.status':
                    return dict(id=p['id'],state='IDLE',sequence=1,terminal=False,scope={'orderId':'original-PO'},active=False,summary=None,manifest=None,error=None)
                return super().request(action,**kwargs)
        if kind=='purchase':
            from aoxiang_purchase_ui import PurchaseDialog
            dialog=PurchaseDialog(None,self.host,self.f.f.h,client_factory=Client)
        else:
            from aoxiang_receipt_ui import ReceiptDialog
            dialog=ReceiptDialog(None,self.host,self.f.f.h,client_factory=Client)
        self.dialogs.append(dialog);dialog.show()
        self.wait(lambda:dialog.current is not None and dialog.worker is None)
        return dialog

    def text(self,d):
        from PySide6.QtWidgets import QLabel
        return '\n'.join(w.text() for w in d.findChildren(QLabel))

    def tearDown(self):
        import shiboken6
        for d in self.dialogs:
            d.close();self.wait(lambda:d._can_close and d.worker is None);shiboken6.delete(d)
        self.warning.stop();self.f.tearDown()

    def test_bound_purchase_updated_payment_is_visible_without_mutating_original(self):
        self.f.f.save('payment',[{'付款单商品数量':8,'实际金额':1.23}])
        before=self.f.f.view()
        d=self.open()
        self.assertIn('采购依据待核',self.text(d))
        self.assertIn('付款单已更新',self.text(d))
        self.assertIn('original-PO',self.text(d))
        self.assertEqual(d.table.item(0,2).text(),'7')
        self.assertEqual(d.table.item(0,3).text(),'0.99')
        self.assertEqual(self.f.f.view(),before)
        self.assertFalse(any(a in ('purchase.start','purchase.reserve','binding.confirm') for a,_ in d.client.calls))

    def test_current_payment_matches_and_prepared_upload_remains_available(self):
        d=self.open(state='RESERVED')
        self.assertIn('与当前已确认付款一致',self.text(d))
        self.assertTrue(d.upload_btn.isEnabled())
        self.assertEqual(self.errors,[])

    def test_prepared_payment_update_disables_upload_but_keeps_original_preview(self):
        d=self.open(state='RESERVED')
        self.f.f.save('payment',[{'付款单商品数量':8,'实际金额':1.23}])
        d.load();self.wait(lambda:d.worker is None)
        self.assertIn('付款单已更新',self.text(d))
        self.assertFalse(d.upload_btn.isEnabled())
        self.assertEqual(d.package,self.package)

    def test_receipt_window_shows_stale_source_without_starting_platform_query(self):
        self.f.f.save('payment',[{'付款单商品数量':8,'实际金额':1.23}])
        before=self.f.f.view()
        d=self.open('receipt')
        self.assertIn('采购依据待核',self.text(d))
        self.assertIn('付款单已更新',self.text(d))
        self.assertEqual(self.f.f.view(),before)
        self.assertFalse(any(a in ('receipt.start','purchase.start','receipt.synced') for a,_ in d.client.calls))

    def test_missing_or_modified_frozen_basis_is_review_not_current(self):
        d=self.open(state='RESERVED')
        path=Path(self.package['plan_file'])
        path.write_text('{}',encoding='utf-8')
        d.load();self.wait(lambda:d.worker is None)
        self.assertIn('依据文件已变化',self.text(d))
        self.assertFalse(d.upload_btn.isEnabled())
        path.unlink()
        d.load();self.wait(lambda:d.worker is None)
        self.assertIn('采购依据待核',self.text(d))
        self.assertFalse(d.upload_btn.isEnabled())

    def test_supplier_change_visible_and_rechecking_does_not_reprice(self):
        d=self.open(state='RESERVED')
        # Change only the current configuration response; preserve the actual frozen payment.
        with patch('aoxiang_purchase_ui.Directory.suppliers',return_value={'河北仓':'CHANGED','山东仓':'ZXC2025'}):
            d.load();self.wait(lambda:d.worker is None)
        self.assertIn('供应商配置已变化',self.text(d))
        self.assertFalse(d.upload_btn.isEnabled())
        self.assertEqual(d.table.item(0,3).text(),'0.99')

    def test_background_basis_check_keeps_ui_responsive_and_close_waits(self):
        import threading
        from PySide6.QtCore import QTimer
        import aoxiang_purchase_ui as ui
        d=self.open(state='RESERVED');started,release=threading.Event(),threading.Event()
        original=ui.inspect_basis
        def slow(*args):
            started.set()
            if not release.wait(5):raise RuntimeError('test release timeout')
            return original(*args)
        beats=[];timer=QTimer(d);timer.setInterval(10);timer.timeout.connect(lambda:beats.append(True));timer.start()
        try:
            with patch('aoxiang_purchase_ui.inspect_basis',side_effect=slow):
                d.load();self.wait(lambda:started.is_set())
                d.close();self.wait(lambda:len(beats)>=4)
                self.assertFalse(d._can_close)
                release.set();self.wait(lambda:d._can_close and d.worker is None)
        finally:
            release.set();timer.stop()
