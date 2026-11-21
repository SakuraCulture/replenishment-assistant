from contextlib import closing
from datetime import date, timedelta
from importlib.util import find_spec
import os
from threading import Event, get_ident
from types import SimpleNamespace
from unittest import TestCase, skipUnless
from unittest.mock import patch

import jst_auto_order as automatic
import jst_auto_order_store as tasks
import jst_batches as batches
import jst_fulfillment_jobs as payment_jobs
import jst_one_click as operation
from jst_one_click_fixture import Component, WAREHOUSES, SKUS
import jst_order_store as generations
import order_lifecycle as lifecycle
import payment_shop_names
import purchases
from runtime_support import Cancelled
import test_jst_auto_order as fixture
import test_lifecycle
import work_repository as wr


class OneClick(TestCase):
    def setUp(self):
        seed = test_lifecycle.LifecycleTests()
        seed.setUp()
        self.addCleanup(seed.tearDown)
        self.enterContext(patch('storage_chunks.backup_before_write'))
        self.prepare_case(seed)

    def prepare_case(self, seed):
        self.seed = seed
        self.path = seed.path
        self.heads = seed.heads
        self.component = Component(seed.root/'component')
        with closing(seed.work.connect()) as db, db:
            yesterday = (date.fromisoformat(wr.today())-timedelta(days=1)).isoformat()
            db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',
                       (yesterday, 'T', 'normal_model', 'old-oid', 'BH-HISTORY-ZC', 1))
            db.execute('INSERT INTO normal_revisions SELECT ?,revision,digest,saved,payload FROM normal_revisions WHERE oid=?',
                       ('old-oid', 'oid'))
        historic = [h for h in purchases.targets(self.path) if h['oid'] == 'old-oid']
        self.assertEqual([h['warehouse'] for h in historic], list(WAREHOUSES))
        self.save_return(historic + self.heads)
        for wh in WAREHOUSES:
            self.configure(wh)
        with self.sync_once(100):
            for wh in WAREHOUSES:
                self.refresh(wh)
        plan = batches.preview_new(self.path, historic)
        receipt = batches.execute(self.path, plan)
        self.assertFalse(receipt['errors'])
        with closing(wr.read_connection(self.path)) as db:
            selected = [{'head': h, 'gid': generations.active(db, h)} for h in historic]
        prepared = automatic.prepare(self.path, selected)
        self.assertEqual(len(prepared['ready']), 2, prepared)
        history = automatic.process(self.path, prepared['ready'], fixture.Portal(), 'preview')
        self.assertTrue(all(r['ok'] for r in history), history)
        self.historic_keys = prepared['ready']
        self.component.calls.clear()

    def configure(self, wh):
        data = {'names': {'T': '测试店'}, 'records': {}}
        payment_shop_names.save(self.path, payment_shop_names.prepare(
            data, [{'store': 'T', 'warehouse': wh, 'value': wh+'合成店铺'}]))
        automatic.save_profile(self.path, 'T', wh, fixture.CONTACT, None)

    def save_return(self, heads, quantity=40):
        return self.seed.save('return', [
            {'补货单号': h['number'], '供货仓': h['warehouse'], '商品编码': SKUS[h['warehouse']],
             '门店确认数量': quantity} for h in heads])

    def sync_once(self, stock):
        return patch.object(self.component, 'stock', dict.fromkeys(WAREHOUSES, stock))

    def refresh(self, wh):
        self.component.start()
        try:
            return operation.catalog_sync.run_sync(self.component, self.path.parent, self.path, [wh], 'allocation_baseline')
        finally:
            self.component.close(timeout=1)

    def preflight(self, heads=None):
        return operation.preflight(self.path, self.heads if heads is None else heads)

    def run_approved(self, ready, cancel=None):
        return operation.execute(self.path, ready, self.component, cancel=cancel)

    def test_dual_warehouse_uses_fresh_stock_and_leaves_payment_for_human(self):
        plan = self.preflight()
        self.assertEqual([r['mode'] for r in plan['ready']], ['allocate', 'allocate'], plan)
        self.assertEqual(self.component.calls, [])
        result = self.run_approved(plan['ready'])
        self.assertEqual([r['status'] for r in result], ['待人工确认付款']*2, result)
        self.assertEqual([r['internal'] for r in result], ['17001', '17002'])
        self.assertEqual([c[1] for c in self.component.calls if c[0] == 'sync.start'], [('河北仓',), ('山东仓',)])
        self.assertEqual([c[1] for c in self.component.calls if c[0] == 'order.import'], list(WAREHOUSES))
        self.assertCountEqual(payment_jobs.waiting_review(self.path), [r['online'] for r in result])
        with closing(wr.read_connection(self.path)) as db:
            gids = [generations.active(db, h) for h in self.heads]
            fresh = [generations.read_generation(db, gid)[0] for gid in gids]
            self.assertEqual(fresh[0]['batch_id'], fresh[1]['batch_id'])
            for row, h, qty in zip(result, self.heads, (17, 12)):
                saved = tasks.read(db, row['online'])
                self.assertEqual(saved['payload']['lines'], [[SKUS[h['warehouse']], str(qty), '2']])
                self.assertEqual(saved['internal'], row['internal'])
                self.assertIsNone(lifecycle.current(db, h, 'payment'))
                self.assertEqual(lifecycle.snapshot(self.path, h)['status'], '待付款')
            self.assertTrue(all(tasks.read(db, k)['stage'] == 'PREVIEWED' for k in self.historic_keys))
        self.assertFalse(self.component.running)

    def test_failed_refresh_has_no_old_stock_fallback_and_zero_never_imports(self):
        self.component.no_stock_receipt.add('河北仓')
        self.component.stock['山东仓'] = 0
        result = self.run_approved(self.preflight()['ready'])
        self.assertEqual([r['status'] for r in result], ['库存刷新待核', '分配为0'], result)
        self.assertFalse(self.component.orders)
        self.assertFalse(any(c[0].startswith('order.') for c in self.component.calls))

    def test_lost_import_reply_recovers_same_online_order_without_refresh_or_import(self):
        self.component.drop_import_response = True
        selected = self.heads[:1]
        first, = self.run_approved(self.preflight(selected)['ready'])
        self.assertEqual(first['status'], '导入结果待核', first)
        self.assertIsNone(first['internal'])
        self.component.calls.clear()
        recovery = self.preflight(selected)
        self.assertEqual([r['mode'] for r in recovery['ready']], ['recover'], recovery)
        recovered, = self.run_approved(recovery['ready'])
        self.assertEqual((recovered['status'], recovered['online'], recovered['internal']),
                         ('待人工确认付款', first['online'], '17001'), recovered)
        self.assertEqual([c[0] for c in self.component.calls], ['order.lookup', 'order.payment_detail'])
        self.assertEqual(payment_jobs.waiting_review(self.path), [first['online']])

    def test_delivery_change_after_approval_or_during_refresh_blocks_original_selection(self):
        ready = self.preflight()['ready']
        with closing(wr.read_connection(self.path)) as db:
            profiles = [tasks.profile(db, 'T', wh) for wh in WAREHOUSES]
        automatic.save_profile(self.path, 'T', '河北仓', {**fixture.CONTACT, 'address': '合成改址'}, profiles[0]['version'])
        self.component.after_sync = lambda: automatic.save_profile(
            self.path, 'T', '山东仓', {**fixture.CONTACT, 'address': '合成改址'}, profiles[1]['version'])
        result = self.run_approved(ready)
        self.assertEqual([r['status'] for r in result], ['待核', '待核'], result)
        self.assertTrue(all('尚未通过平台预览' in r['message'] for r in result))
        self.assertEqual(self.component.calls, [('sync.start', ('山东仓',))])
        with closing(wr.read_connection(self.path)) as db:
            self.assertTrue(all(not generations.active(db, h) for h in self.heads))

    def test_preview_error_leaves_saved_allocation_and_never_dispatches_submit(self):
        self.component.missing_preview.add('河北仓')
        hb = self.heads[:1]
        result, = self.run_approved(self.preflight(hb)['ready'])
        self.assertEqual(result['status'], '预览待核', result)
        self.assertEqual([c[0] for c in self.component.calls], ['sync.start', 'order.preview'])
        previous, = automatic.process(self.path, [result['online']], fixture.Portal(), 'preview')
        self.assertEqual(previous['stage'], 'PREVIEWED', previous)
        self.component.calls.clear()
        result, = self.run_approved(self.preflight(hb)['ready'])
        self.assertEqual(result['status'], '预览待核', result)
        self.assertEqual([c[0] for c in self.component.calls], ['order.preview'])
        self.component.calls.clear()
        self.component.missing_preview.clear()
        retry = self.preflight(hb)
        self.assertEqual([r['mode'] for r in retry['ready']], ['order'], retry)
        result, = self.run_approved(retry['ready'])
        self.assertEqual(result['status'], '待人工确认付款', result)
        self.assertNotIn('sync.start', [c[0] for c in self.component.calls])

    def test_other_active_allocation_batch_keeps_its_members_and_epoch(self):
        batch = batches.preview_new(self.path, self.heads[1:])
        batches.register(self.path, batch)
        bid = batch['payload']['batch_id']
        def state():
            with closing(wr.read_connection(self.path)) as db:
                return batches.context(db, bid, load_stock=False)
        before = state()
        failed, = self.run_approved(self.preflight(self.heads[:1])['ready'])
        self.assertEqual(failed['status'], '分配待核', failed)
        self.assertIn('另有分配批次正在处理', failed['message'])
        self.assertEqual(state(), before)
        self.assertFalse(self.component.orders)

    def test_corrected_account_requires_new_verification_before_refresh(self):
        plan = self.preflight(self.heads[:1])
        self.component.accounts[0]['bindingVersion'] = 8
        blocked, = self.run_approved(plan['ready'])
        self.assertEqual(blocked['status'], '待核', blocked)
        self.assertIn('仓账号或绑定版本已变化', blocked['message'])
        self.assertFalse(self.component.calls)

    def test_cancel_before_dispatch_does_not_touch_browser_or_business_records(self):
        ready = self.preflight()['ready']
        cancel = Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            self.run_approved(ready, cancel)
        self.assertEqual(self.component.calls, [])
        with closing(wr.read_connection(self.path)) as db:
            self.assertTrue(all(not generations.active(db, h) for h in self.heads))

    @skipUnless(find_spec('PySide6'), 'optional Qt')
    def test_results_page_confirms_only_checked_warehouse_and_updates_on_gui_thread(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton, QTableView, QWidget
        from jst_results_ui import ResultsPage
        import shiboken6

        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        app = QApplication.instance() or QApplication([])
        host = QWidget()
        host.repository = SimpleNamespace(folder=self.seed.root)
        host.work_repository = self.seed.work
        host.closing = False
        adopted = []
        host.current_warehouse_adopted = lambda c: adopted.append((c['warehouse'], get_ident()))
        page = ResultsPage(host, parent=host)
        page.show()
        observed = []
        errors = []
        accept = False
        timer = QTimer(host)

        def handle_dialog():
            dialog = QApplication.activeModalWidget()
            if not dialog:
                return
            title = dialog.windowTitle()
            if isinstance(dialog, QMessageBox):
                errors.append(dialog.text())
                dialog.reject()
            elif title in ('确认一键分配下单', '一键作业结果'):
                table = dialog.findChild(QTableView)
                model = table.model()
                observed.append((title, list(model.rows)))
                label = ('确认本次作业' if accept else '取消') if title == '确认一键分配下单' else '关闭'
                button = next((b for b in dialog.findChildren(QPushButton) if label in (b.text(), b.accessibleName())), None)
                if button is None:
                    errors.append(f'{title} 缺少 {label} 按钮')
                    dialog.reject()
                else:
                    button.click()

        timer.timeout.connect(handle_dialog)
        try:
            for _ in range(200):
                if page.data is not None and not page.reading:
                    break
                QTest.qWait(25)
            self.assertEqual(len(page.records), 2, page.status.text())
            self.assertFalse(page.one_click_btn.isEnabled())
            page.warehouse.setCurrentText('山东仓')
            page.select_visible()
            self.assertTrue(page.one_click_btn.isEnabled())
            self.assertIn('1单', page.one_click_btn.text())
            with patch('catalog_session_ui.manager', return_value=SimpleNamespace(session=self.component)) as manager:
                timer.start(20)
                page.one_click_btn.click()
                manager.assert_not_called()
                self.assertEqual(self.component.calls, [])
                for _ in range(200):
                    if not page.reading:
                        break
                    QTest.qWait(25)
                accept = True
                page.one_click_btn.click()
                manager.assert_called_once_with(host)
            self.assertFalse(errors, errors)
            self.assertEqual([title for title, _ in observed], ['确认一键分配下单']*2+['一键作业结果'])
            self.assertTrue(all(len(rows) == 1 and rows[0][1] == '山东仓' for _, rows in observed))
            self.assertEqual(observed[-1][1][0][3:5], ('17001', '待人工确认付款'))
            self.assertEqual(adopted, [('山东仓', get_ident())])
        finally:
            timer.stop()
            page.shutdown()
            host.close()
            app.processEvents()
            shiboken6.delete(host)
