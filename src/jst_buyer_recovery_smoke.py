"""Native window recovery and payment review with an isolated order and replies."""
import json
import os
import sys
import traceback
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox


def start_smoke(app, win, args):
    out = Path(args[0]).resolve()
    phase = args[1]
    fixture = json.loads((out/'synthetic.json').read_text(encoding='utf-8'))
    if (phase not in ('recover', 'reopen', 'cancel-redo', 'cancel-reopen', 'shipping') or fixture.get('synthetic') is not True
            or fixture.get('live_platform') is not False or fixture.get('case') != 'jst-buyer-recovery'
            or Path(os.environ.get('REPLENISHMENT_DATA_DIR', '')).resolve() != out/'settings'
            or win.work_repository.path.resolve() != out/'settings/workbench.sqlite3'):
        raise ValueError('需要专用隔离验收资料目录')

    import catalog_session_ui
    import jst_fulfillment as payment
    import jst_fulfillment_store as sources
    import order_lifecycle as lifecycle
    from catalog_sync_client import Client
    from jst_auto_order_store import read
    from jst_auto_order_ui import Orders
    from version_info import diagnostic
    from work_repository import read_connection

    win.jst_generator.stopped = True
    win.jst_generator.timer.stop()
    win.aoxiang_workflows.stopping = True
    win.aoxiang_workflows.timer.stop()
    win.browser_status.paused = True
    win.browser_status.initial.stop()
    win.browser_status.timer.stop()
    win.jst_fulfillment_runner.timer.stop()
    win.scheduled.timer.stop()
    path = win.work_repository.path
    online = fixture['online']
    receipt = json.loads((out/'receipt.json').read_text(encoding='utf-8'))
    warehouse = fixture.get('warehouse', '山东仓')
    use_frozen_preview = fixture.get('use_frozen_preview') is True
    messages, calls = [], []
    buyer = {'check':'unavailable'}

    class Lease:
        def start(self):
            return True

        def close(self, timeout):
            return True

        def request(self, action, *, spec, **extra):
            assert action in ('order.lookup', 'order.payment_detail'), action
            assert spec['online'] == online and spec['binding'] == receipt['binding']
            assert spec['warehouse'] == warehouse and spec['contact'] == {'buyer': fixture['buyer']}
            calls.append(action)
            if action == 'order.lookup':
                assert extra['identity_only'] is True and spec['internal'] in (None, receipt['internal'])
                return {**(receipt if buyer.get('full_detail') else {}), **fixture['lookup'],
                        'buyer_check': buyer['check'], 'contact_ok': buyer['check'] == 'verified'}
            assert spec['internal'] == receipt['internal']
            return receipt

    def state():
        with closing(read_connection(path)) as db:
            return read(db, online), sources.current(db, online), sources.history(db, online)

    def snapshot(dialog, title):
        dialog.table.scrollToItem(dialog.table.item(0, 13))
        assert dialog.grab().save(str(out/(title+'.png')))

    def run():
        report = {'passed': False, 'phase': phase, 'frozen_exe': bool(getattr(sys, 'frozen', False)),
                  'build': diagnostic(), 'synthetic': True, 'live_platform': False, 'calls': calls}
        original = catalog_session_ui.manager
        qtimer = QTimer(win)
        dialog = None

        def dismiss_error():
            box = app.activeModalWidget()
            if isinstance(box, QMessageBox):
                if phase == 'cancel-redo' and box.windowTitle() == '确认沿原仓单重做':
                    box.grab().save(str(out/'确认沿原仓单重做.png'))
                    box.button(QMessageBox.StandardButton.Yes).click()
                    return
                messages.append(box.text())
                box.reject()

        qtimer.timeout.connect(dismiss_error)
        try:
            if phase in ('recover', 'cancel-redo', 'shipping'):
                component = Client(out/'component-startup')
                try:
                    component.start()
                    assert component.request('order.capabilities')['payment_detail'] is True
                    report['component_started'] = True
                finally:
                    assert component.close()

            catalog_session_ui.manager = lambda _: SimpleNamespace(session=SimpleNamespace(acquire=Lease))
            dialog = Orders(win, win, [online])
            dialog.show()
            qtimer.start(25)
            if phase in ('cancel-redo', 'cancel-reopen'):
                from jst_cancellation import begin_redo
                from jst_order_store import active, locked
                if phase == 'cancel-redo':
                    before, _, _ = state()
                    assert before['internal'] is None and dialog.redo_btn.isEnabled()
                    buyer['full_detail'] = True
                    dialog.redo_btn.click()
                    assert dialog.redo_target and calls == ['order.lookup'], messages
                order, _, versions = state()
                head = order['payload']['head']
                assert order['historical'] and order['internal'] == receipt['internal'] and order['stage'] == 'CANCELLED'
                assert not versions and not messages
                redo = begin_redo(path, online)
                assert redo['online'] != online and redo['released'] == {sku: qty for sku, qty, _ in order['payload']['lines']}
                with closing(read_connection(path)) as db:
                    assert active(db, head) is None and locked(db, head) is None
                    assert db.execute('SELECT count(*) FROM jst_cancellations WHERE online=? AND successor=?', (online, redo['online'])).fetchone() == (1,)
                    assert db.execute("SELECT count(*) FROM jst_events WHERE action='原取消单释放占用并登记后继'").fetchone() == (1,)
                report.update(passed=True, warehouse=warehouse, internal=order['internal'],
                              successor=redo['online'], released=redo['released'])
                return
            assert dialog.buttons['recover'].isEnabled() and not dialog.buttons['submit'].isEnabled()
            if phase in ('recover', 'shipping'):
                for outcome, hint in (('unavailable','原单身份待核'),('mismatch','完整买家账号与原任务不同')):
                    buyer['check'] = outcome
                    dialog.buttons['recover'].click()
                    original_order, current, versions = state()
                    assert original_order['stage'] == 'IMPORT_UNKNOWN' and original_order['internal'] is None
                    assert hint in dialog.note.text() and not current and not versions
                    snapshot(dialog, outcome)
                buyer.update(check='unavailable' if use_frozen_preview else 'verified', full_detail=use_frozen_preview)
                for _ in range(2):
                    dialog.buttons['recover'].click()
                assert calls == ['order.lookup', 'order.lookup', 'order.lookup', 'order.payment_detail',
                                 'order.lookup', 'order.payment_detail']
            else:
                assert not calls

            order, current, versions = state()
            assert order['internal'] == receipt['internal'] and current['state'] == 'READY' and len(versions) == 1
            assert dialog.table.item(0,10).text() == order['internal']
            assert dialog.table.item(0,12).text() == '待人工确认付款'
            assert not lifecycle.snapshot(path, order['payload']['head'])['present']['payment']
            plan, summary = payment.prepare_review(path, [online])
            assert summary[0]['quantity'] == fixture.get('quantity', '10503')
            assert summary[0]['totals']['payable'] == fixture.get('amount', '24554.61')
            assert receipt['detail_count'] == len(order['payload']['lines']) == fixture.get('sku_count', 582)
            assert not messages, messages
            snapshot(dialog, phase+'-待人工确认付款')
            if phase == 'shipping':
                import daily_batch
                from lifecycle_home_ui import LifecycleWidget
                head = order['payload']['head']
                lifecycle.commit(path, plan)
                cache = out/'shipping.sqlite3'
                overview = {'status':'待发货','platform_tracking':True}
                inbox = {'stage_view':'receipt','stage_filter':'待处理'}
                assert daily_batch.read(path, cache, inbox)['count'] == 0
                widget = LifecycleWidget(win)
                try:
                    widget.ready(daily_batch.read(path, cache, overview))
                    widget.show()
                    action = widget.table.cellWidget(0,2)
                    assert action.text() == '核对聚水潭发货状态' and action.isEnabled()
                    assert widget.grab().save(str(out/'待发货.png'))
                    fixture['lookup']['native_status'] = '发货中(打单拣货)'
                    action.click()
                    received = daily_batch.read(path, cache, inbox)
                    assert received['count'] == 1 and received['records'][0]['number'] == head['number']
                    assert lifecycle.snapshot(path, head)['status'] == '待收货'
                    assert len(lifecycle.history(path, head, 'payment')) == 1 and not messages
                    report.update(receipt_status_before='待发货', receipt_status_after='待收货')
                finally:
                    widget.reader.shutdown()
                    widget.deleteLater()
            report.update(passed=True, warehouse=warehouse, used_frozen_preview=use_frozen_preview,
                          synthetic_internal=order['internal'], payment_candidate=current['digest'],
                          sku_count=receipt['detail_count'], quantity=summary[0]['quantity'],
                          amount=summary[0]['totals']['payable'], payment_confirmed=phase == 'shipping')
        except BaseException:
            report['error'] = traceback.format_exc()
        finally:
            qtimer.stop()
            if dialog is not None:
                dialog.reject()
                dialog.deleteLater()
            catalog_session_ui.manager = original
            with (out/(phase+'-report.json')).open('x', encoding='utf-8') as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            win.job_dirty = False
            win._discard_close = True
            if report['passed']:
                win.close()
            else:
                app.exit(1)

    QTimer.singleShot(0, run)
