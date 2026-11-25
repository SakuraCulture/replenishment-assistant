"""UI protocol checks; business expectations live in test_jst, not Qt callbacks."""
import os,sys,tempfile,unittest,importlib.util
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))

@unittest.skipUnless(importlib.util.find_spec('PySide6'),'optional Qt')
class JstUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,REPLENISHMENT_DATA_DIR=self.tmp.name);self.env.start();self.reader=patch('history_tasks.HistoryReader.request');self.reader_mock=self.reader.start()
        from application_window import ApplicationWindow
        self.win=ApplicationWindow()
    def tearDown(self):
        import shiboken6
        self.win._discard_close=True;self.win.close();self.app.processEvents();shiboken6.delete(self.win);self.reader.stop();self.env.stop();self.tmp.cleanup()
    def test_overview_has_progress_entries_and_no_execution_toolbar(self):
        w=self.win.followup
        from PySide6.QtWidgets import QPushButton,QComboBox
        texts=[b.text() for b in w.findChildren(QPushButton)]
        self.assertIn('聚水潭导入单',texts)
        self.assertTrue({'进入回传处理','进入运营签发','进入付款核对','进入收货核对'}.isdisjoint(texts))
        self.assertNotIn('生成勾选聚水潭订单',texts);self.assertNotIn('确认勾选已下单并锁定',texts)
        self.assertNotIn('全部锁状态',[c.itemText(0) for c in w.findChildren(QComboBox)])
        self.assertEqual(list(w.cards),['待回传','待运营签发','待付款','待发货','待收货','已结束'])
        self.assertEqual(w.start.text(),w.end.text())
        w.query();request=self.reader_mock.call_args.args[0];self.assertEqual(request['kind'],'daily_batch');self.assertNotIn('lock',request)
    def test_progress_cards_route_selected_orders_or_overview_filters(self):
        import order_lifecycle
        from PySide6.QtCore import QTimer
        w = self.win.followup
        w.start.setText('2026-11-11'); w.end.setText('2026-11-17')
        w.search.setText('门店T'); w.warehouse_filter.setCurrentText('山东仓')
        heads = [dict(oid='T1', warehouse=wh, number='BH-T-'+code, revision=1)
                 for wh, code in [('山东仓','SD'),('河北仓','HB')]]
        routes = {'待回传':'return', '待运营签发':'operations', '待付款':'payment', '待收货':'receipt'}
        for selected in ([], heads):
            w.checked = {order_lifecycle.key(h):h for h in selected}
            for title, stage in routes.items():
                with self.subTest(title=title, selected=bool(selected)):
                    observed = []
                    def inspect():
                        dialog = self.app.activeModalWidget()
                        panel = dialog.panel
                        observed.append((dialog.stage, panel.fixed_scope, list(panel.checked.values()),
                                         panel.start.text(), panel.end.text(), panel.search.text(), panel.warehouse_filter.currentText()))
                        dialog.reject()
                    QTimer.singleShot(15, inspect)
                    w.cards[title].click()
                    self.assertEqual(observed, [(stage, selected or None, selected, '2026-11-11','2026-11-17','门店T','山东仓')])
        with patch.object(w,'open_stage') as opened:
            w.cards['已结束'].click()
            opened.assert_not_called()
            self.assertEqual(w.status.currentText(),'已结束')
    def test_stage_workspace_has_only_its_own_actions_and_keeps_overview(self):
        from PySide6.QtWidgets import QPushButton
        from lifecycle_home_ui import StageDialog
        w=self.win.followup;w.start.setText('2026-10-30');w.search.setText('门店A');w.checked={'sentinel':1}
        for stage,primary,absent in [('return','导入门店回传单','导入门店付款单'),('payment','导入门店付款单','导入门店实际收货单'),('receipt','导入门店实际收货单','生成勾选聚水潭订单')]:
            with self.subTest(stage=stage):
                d=StageDialog(w,stage);self.app.processEvents();p=d.panel
                texts=[b.text() for b in p.findChildren(QPushButton)];self.assertIn(primary,texts);self.assertNotIn(absent,texts)
                self.assertEqual(p.start.text(),'2026-10-30');self.assertEqual(p.search.text(),'门店A')
                p.search.setText('本环节独立筛选');d.reject();d.deleteLater();self.app.processEvents()
                self.assertEqual(w.search.text(),'门店A');self.assertEqual(w.checked,{'sentinel':1})
    def test_result_page_inherits_explicit_overview_scope(self):
        from lifecycle_home_ui import StageDialog
        w=self.win.followup;w.start.setText('2026-10-30');w.search.setText('门店A');w.warehouse_filter.setCurrentText('河北仓')
        d=StageDialog(w,'jst');self.app.processEvents();p=d.panel
        self.assertEqual(p.day.text(),'2026-10-30');self.assertEqual(p.end.text(),w.end.text());self.assertEqual(p.search.text(),'门店A');self.assertEqual(p.warehouse.currentText(),'河北仓')
        self.assertNotIn('下单锁定',[p.table.headerItem().text(i) for i in range(p.table.columnCount())])
        self.assertIsNone(p.scope);self.assertEqual(p.all_btn.text(),'导出当前筛选（0份）');self.assertTrue(d.property('jstResults'))
        self.assertEqual(self.reader_mock.call_args.args[0]['kind'],'jst_results')
        d.reject();d.deleteLater();self.app.processEvents();self.assertEqual(w.search.text(),'门店A')
        from PySide6.QtWidgets import QPushButton
        w.checked={'unrelated':{'day':'2026-10-30'}}
        with patch.object(w,'open_stage') as opened:
            next(b for b in self.win.findChildren(QPushButton) if b.text()=='查看分配结果').click()
            opened.assert_called_once_with('jst')
    def test_generator_has_distinct_qt_finished_signal_and_worker_callback(self):
        from jst_ui import GenerationDialog
        from PySide6.QtWidgets import QPushButton
        with patch.object(GenerationDialog,'load_catalogs'):
            d=GenerationDialog(self.win,self.win,[]);self.app.processEvents()
            self.assertFalse(d.auto_export.isChecked())
            self.assertFalse(any('并锁定' in b.text() for b in d.findChildren(QPushButton)))
            self.assertTrue(callable(d.worker_finished));self.assertTrue(hasattr(d.finished,'connect'))
            self.assertFalse(d.generate_btn.isEnabled());self.assertFalse(d._completed)
            d.reject();self.assertTrue(d._completed);d.deleteLater();self.app.processEvents()
    def test_other_order_actions_only_offer_reconciliation_for_existing_marked_orders(self):
        from jst_results_ui import ResultsPage
        from PySide6.QtWidgets import QPushButton
        page=ResultsPage(self.win,parent=self.win)
        page.records=[{'head':{'oid':'A','warehouse':'河北仓'},'gid':'legacy','locked':True},
                      {'head':{'oid':'B','warehouse':'山东仓'},'gid':'new','locked':False}]
        page.data={'records':page.records}
        def inspect(dialog):
            buttons={b.text():b for b in dialog.findChildren(QPushButton)}
            self.assertNotIn('确认已下单并锁定',buttons)
            legacy=buttons.get('核对历史下单标记（1单）')
            if ('A','河北仓') in page.chosen:
                self.assertIsNotNone(legacy);legacy.click()
            else:self.assertIsNone(legacy)
            return 0
        try:
            with patch('jst_results_ui.QDialog.exec',inspect),patch.object(page,'refresh'),patch('jst_ui.reconcile_legacy_orders',return_value=True) as reconcile:
                page.chosen={('B','山东仓')};page.tools();reconcile.assert_not_called()
                page.chosen.add(('A','河北仓'));page.tools()
                self.assertEqual(reconcile.call_args.args[2],['legacy'])
        finally:
            page.shutdown();page.deleteLater();self.app.processEvents()
    def test_legacy_reconciliation_preserves_occupancy_and_rejects_confirmed_payment(self):
        from contextlib import closing
        from types import SimpleNamespace
        import jst_orders as orders
        import jst_ui
        import order_lifecycle as lifecycle
        from test_jst import Service
        fixture=Service();fixture.setUp()
        try:
            _,gid=fixture.generated();path=fixture.path;head=fixture.h
            orders.lock(path,[gid]);before=fixture.balances()
            host=SimpleNamespace(work_repository=fixture.f.work)
            with patch('jst_ui.QInputDialog.getText',return_value=('合成原单已撤销',True)),patch('jst_ui.confirm',return_value=True),patch('jst_ui.run',side_effect=lambda parent,fn,args,name:fn(*args)),patch('jst_ui.warn') as warn:
                self.assertTrue(jst_ui.reconcile_legacy_orders(self.win,host,[gid]))
                with closing(fixture.f.work.connect()) as db:self.assertIsNone(orders.locked(db,head))
                self.assertEqual(fixture.balances(),before)
                orders.lock(path,[gid]);lifecycle.commit(path,orders.payment_plan(path,gid))
                self.assertFalse(jst_ui.reconcile_legacy_orders(self.win,host,[gid]));warn.assert_called_once()
                with closing(fixture.f.work.connect()) as db:self.assertEqual(orders.locked(db,head),gid)
                self.assertEqual(fixture.balances(),before)
        finally:fixture.tearDown()
    def test_commit_boundary_disables_stop(self):
        from jst_ui import GenerationDialog
        with patch.object(GenerationDialog,'load_catalogs'):
            d=GenerationDialog(self.win,self.win,[]);self.app.processEvents();d.boundary(True)
            self.assertFalse(d.stop_btn.isEnabled());self.assertTrue(d.committing)
            d.boundary(False);self.assertTrue(d.stop_btn.isEnabled());d.reject();d.deleteLater();self.app.processEvents()
    def test_cancelled_batch_summary_only_counts_committed_orders(self):
        from jst_ui import GenerationDialog
        from test_jst_review import plans
        with patch.object(GenerationDialog,'load_catalogs'):
            d=GenerationDialog(self.win,self.win,[]);self.app.processEvents();rows=plans()
            for i,p in enumerate(rows):p['gid']='G'+str(i)
            d.plan={'payload':{'plans':rows}}
            d.generation_done({'generated':['G0'],'zero':[],'errors':[],'cancelled':True})
            self.assertEqual(len(d.review.groups),1);self.assertEqual(len(d.review.groups[0]['plans']),1)
            self.assertEqual(d.review.groups[0]['metrics']['allocated'],63)
            d.invalidate();self.assertFalse(d.review.groups)
            d.reject();d.deleteLater();self.app.processEvents()
    def test_generation_ready_and_old_callback_do_not_mark_paid(self):
        from jst_ui import GenerationDialog
        with patch.object(GenerationDialog,'load_catalogs'):
            d=GenerationDialog(self.win,self.win,[]);self.app.processEvents();d.auto_export.setChecked(False)
            d.saved_order({'gid':'G1','number':'N1','positive':True});d.saved_order({'gid':'G1','number':'N1','positive':True})
            self.assertEqual(d.completed.count(),1);self.assertEqual(d.saved,['G1']);self.assertEqual(d.export_queue,[])
            d.reject();d.deleteLater();self.app.processEvents()
    def test_close_requested_during_commit_stops_at_next_safe_boundary(self):
        import threading
        from types import SimpleNamespace
        from jst_ui import GenerationDialog
        with patch.object(GenerationDialog,'load_catalogs'):
            d=GenerationDialog(self.win,self.win,[]);self.app.processEvents()
            d.worker=SimpleNamespace(cancel=threading.Event());d.boundary(True);d.reject()
            self.assertTrue(d.close_pending);self.assertFalse(d.worker.cancel.is_set())
            d.boundary(False);self.assertTrue(d.worker.cancel.is_set())
            d.worker=None;d.reject();d.deleteLater();self.app.processEvents()

if __name__=='__main__':unittest.main()
