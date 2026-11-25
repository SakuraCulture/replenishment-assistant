"""Offscreen application widgets with isolated, synthetic business facts."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

from PySide6.QtWidgets import QApplication, QMessageBox, QWidget
from PySide6.QtGui import QFont, QFontDatabase
import jst_fulfillment as service
import jst_fulfillment_jobs as jobs
from jst_fulfillment_runner import Runner, install
from jst_auto_order_ui import Orders
from lifecycle_ui import Review
from jst_fulfillment_ui import make_review
import test_jst_fulfillment as fixtures


class FulfillmentUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion')
        font=Path('C:/Windows/Fonts/msyh.ttc')
        if font.is_file():
            QFontDatabase.addApplicationFont(str(font))
        cls.app.setFont(QFont('Microsoft YaHei',9))

    def setUp(self):
        self.fixture=fixtures.Fulfillment()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.archive()
        self.host=QWidget()
        self.host.work_repository=SimpleNamespace(path=self.fixture.path)
        self.host.repository=SimpleNamespace(folder=self.fixture.path.parent)
        self.host.closing=False
        self.host.worker=None
        self.addCleanup(self.host.deleteLater)

    def test_orders_show_pending_allocation_and_polling_is_initially_off(self):
        dialog=Orders(self.host,self.host,self.fixture.keys)
        try:
            self.assertEqual(dialog.table.rowCount(),1)
            self.assertEqual(dialog.table.item(0,12).text(),'待人工确认付款')
            self.assertTrue(dialog.allocation.isEnabled())
            self.assertFalse(dialog.auto_allocation.isChecked())
        finally:dialog.deleteLater()

    def test_cancel_redo_shows_the_reason_when_cancellation_is_unverified(self):
        dialog=Orders(self.host,self.host,self.fixture.keys)
        self.addCleanup(dialog.deleteLater)
        examples=(
            ([{'ok':False,'stage':'AUDITED','reason':'原单身份待核'}], '原单身份待核'),
            ([{'ok':True,'stage':'AUDITED','reason':'聚水潭当前为“已客审待财审”'}], '已客审待财审'),
            ([], '未取得原单回查结果'),
        )
        for result,reason in examples:
            with self.subTest(reason=reason), patch('catalog_session_ui.manager'), \
                    patch('jst_auto_order_ui.run',return_value=result) as run, \
                    patch('jst_auto_order_ui.confirm') as confirm, patch.object(QMessageBox,'warning') as warning:
                dialog.redo_btn.click()
                run.assert_called_once()
                confirm.assert_not_called()
                warning.assert_called_once()
                parent,title,detail=warning.call_args.args
                self.assertIs(parent,dialog)
                self.assertEqual(title,'取消重做未完成')
                self.assertIn(reason,detail)
                self.assertIn(self.fixture.keys[0],detail)
                self.assertEqual(dialog.note.text(),detail)
                self.assertTrue(dialog.redo_btn.isEnabled())
                self.assertIsNone(dialog.redo_target)

    def test_cancel_redo_leaves_visible_status_when_the_lookup_is_cancelled(self):
        from runtime_support import Cancelled
        dialog=Orders(self.host,self.host,self.fixture.keys)
        self.addCleanup(dialog.deleteLater)
        with patch('catalog_session_ui.manager'), patch('jst_auto_order_ui.run',side_effect=Cancelled), \
                patch('jst_auto_order_ui.confirm') as confirm:
            dialog.redo_btn.click()
            confirm.assert_not_called()
            self.assertIn('本次操作已取消',dialog.note.text())
            self.assertTrue(dialog.redo_btn.isEnabled())
            self.assertIsNone(dialog.redo_target)

    def test_receipt_page_explains_formal_payment_and_the_linked_original(self):
        import daily_batch
        import order_lifecycle as lifecycle
        from lifecycle_home_ui import LifecycleWidget

        cache = self.fixture.root/'receipt-page.sqlite3'
        request = {'stage_view':'receipt','stage_filter':'待处理','platform_tracking':True}
        self.assertEqual(daily_batch.read(self.fixture.path,cache,request)['count'],0)
        plan, _ = service.prepare_review(self.fixture.path,self.fixture.keys)
        lifecycle.commit(self.fixture.path,plan)
        widget = LifecycleWidget(self.host,'receipt')
        self.addCleanup(widget.deleteLater)
        self.addCleanup(widget.reader.shutdown)
        widget.ready(daily_batch.read(self.fixture.path,cache,request))
        self.assertEqual(widget.table.rowCount(),0)
        order = self.fixture.order
        original = {'binding':order['proof']['binding'], 'online':order['online'], 'internal':order['internal'],
                    'native_status':'发货中(打单拣货)', 'found':True, 'contact_ok':True, 'buyer_check':'verified',
                    'items':[{'sku':'0001','qty':'12','price':'2.5','amount':'30'}], 'detail_count':1,
                    'currency':'CNY', 'freight':'0', 'amount':'30'}
        browser = SimpleNamespace(action=Mock(return_value=original))
        [verified] = service.pull_order_payment(self.fixture.path,self.fixture.keys,browser)
        self.assertTrue(verified['payment_locked'], verified)
        widget.ready(daily_batch.read(self.fixture.path,cache,request))
        self.assertEqual(widget.table.rowCount(),1)
        self.assertEqual(widget.table.item(0,3).text(),self.fixture.head['day'])
        self.assertEqual(widget.table.item(0,7).text(),'待收货')
        notice = widget.table.item(0,widget.table.columnCount()-1)
        self.assertIn('已锁定付款 V001',notice.text())
        self.assertIn('聚水潭原单 '+self.fixture.order['internal'],notice.text())
        self.assertNotIn('历史下单标记',notice.text())
        self.assertEqual(notice.toolTip(),notice.text())

    def test_payment_review_cannot_save_until_human_confirms(self):
        plan,overview=service.prepare_review(self.fixture.path,self.fixture.keys)
        dialog=make_review(self.host,plan,overview)
        try:
            dialog.setWindowTitle('合成验收 · 实配回流待人工付款')
            self.assertFalse(dialog.save.isEnabled())
            self.assertIn('人工确认',dialog.save.text())
            self.assertIn('原下单数量',dialog.overview_model.headers)
            dialog.complete.setChecked(True)
            self.assertTrue(dialog.save.isEnabled())
            dialog.complete.setChecked(False)
            self.assertFalse(dialog.save.isEnabled())
            output=os.environ.get('JST_FULFILLMENT_UI_EVIDENCE')
            if output:
                folder=Path(output)
                folder.mkdir(parents=True,exist_ok=True)
                dialog.resize(1280,760)
                dialog.show()
                self.app.processEvents()
                self.assertTrue(dialog.grab().save(str(folder/'合成实配付款核对.png')))
        finally:dialog.deleteLater()

    def test_timer_does_not_open_a_browser_or_login_without_a_retained_idle_session(self):
        runner=Runner(self.host)
        try:
            with patch('catalog_session_ui.manager') as manager:
                runner.tick()
                manager.assert_not_called()
                jobs.configure(self.fixture.path,True)
                manager.return_value.session=SimpleNamespace(lease=None,ready=False,client=None)
                runner.tick()
                self.assertIsNone(runner.worker)
                self.assertIn('未发起新登录',runner.message)
            self.assertTrue(runner.prepare_close())
            self.assertFalse(runner.timer.isActive())
        finally:
            runner.timer.stop()
            runner.deleteLater()

    def test_opening_ready_review_does_not_reexport_or_start_a_browser(self):
        from jst_fulfillment_ui import pull_and_review
        with patch('jst_fulfillment_ui.review', return_value='opened') as review, \
                patch('jst_fulfillment_ui.run') as run, patch('catalog_session_ui.manager') as manager:
            self.assertEqual(pull_and_review(self.host,self.host,self.fixture.keys),'opened')
        review.assert_called_once_with(self.host,self.host,self.fixture.keys)
        run.assert_not_called()
        manager.assert_not_called()

    def test_notifications_survive_restart_and_open_existing_candidates_only(self):
        from PySide6.QtWidgets import QVBoxLayout, QPushButton
        import order_lifecycle as lifecycle
        self.host.followup=QWidget(self.host)
        self.host.followup.query=Mock()
        QVBoxLayout(self.host.followup)
        home=QWidget(self.host)
        layout=QVBoxLayout(home)
        self.host.status=SimpleNamespace(setText=lambda _:None)
        runner=install(self.host,layout)
        try:
            runner.refresh_pending()
            buttons=[w for w in self.host.findChildren(QPushButton) if w.text().startswith('核对回读付款资料')]
            self.assertEqual(len(buttons),2)
            self.assertTrue(all(w.isEnabled() and '（1）' in w.text() for w in buttons))
            with patch('jst_fulfillment_ui.review',return_value=None) as review:
                buttons[0].click()
            review.assert_called_once_with(self.host,self.host,self.fixture.keys)
            self.host.followup.query.assert_called_once_with()
            plan,_=service.prepare_review(self.fixture.path,self.fixture.keys)
            lifecycle.commit(self.fixture.path,plan)
            runner.refresh_pending()
            self.assertEqual(runner.pending,[])
            self.assertTrue(all(not w.isEnabled() for w in buttons))
        finally:
            runner.timer.stop()
            runner.deleteLater()


if __name__=='__main__':unittest.main()
