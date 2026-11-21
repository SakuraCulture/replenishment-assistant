"""Exercise the real one-click and payment windows with an isolated platform fixture."""
from contextlib import closing
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import traceback
from types import SimpleNamespace

from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox, QPushButton, QTableView


def start_smoke(app, win, args):
    out = Path(args[0]).resolve()
    phase = args[1]
    spec = json.loads((out/'synthetic.json').read_text(encoding='utf-8'))
    path = win.work_repository.path
    if (phase not in ('exercise', 'reopen') or spec.get('case') != 'jst-one-click'
            or spec.get('synthetic') is not True or spec.get('live_platform') is not False
            or Path(os.environ.get('REPLENISHMENT_DATA_DIR', '')).resolve() != out/'settings'
            or Path(os.environ.get('LOCALAPPDATA', '')).resolve() != out/'test-appdata'
            or path.resolve() != out/'settings/workbench.sqlite3'):
        raise ValueError('需要专用隔离验收资料目录')

    import catalog_session_ui
    from catalog_sync_client import Client
    import jst_auto_order_store as tasks
    import jst_batches as batches
    import jst_fulfillment as payment
    import jst_fulfillment_store as sources
    import jst_one_click_fixture as fixture
    import jst_order_store as generations
    from lifecycle_home_ui import StageDialog
    import order_lifecycle as lifecycle
    from version_info import diagnostic
    import work_repository as wr

    win.jst_generator.stopped = True
    win.aoxiang_workflows.stopping = True
    win.browser_status.paused = True
    win.browser_status.initial.stop()
    for job in (win.jst_generator, win.aoxiang_workflows, win.browser_status, win.jst_fulfillment_runner, win.scheduled):
        job.timer.stop()

    expected = {row['warehouse']: row for row in spec['orders']}
    port = fixture.Component(out/'fixture-component')
    port.stock = {wh: row['stock'] for wh, row in expected.items()}
    result_rows = {}
    checks = []

    def button(widget, label, *, direct=False):
        return next(b for b in widget.findChildren(QPushButton)
                    if (not direct or b.parentWidget() is widget) and label in (b.text(), b.accessibleName()))

    def snapshot(widget, label):
        assert widget.grab().save(str(out/(phase+'-'+label+'.png')))

    def wait(condition):
        for _ in range(600):
            if condition():
                return
            QTest.qWait(25)
        raise TimeoutError('软件入口读取超时')

    def allocate_page(page):
        wait(lambda: page.data is not None and not page.reading)
        assert not page.one_click_btn.isEnabled()
        assert {r['head']['number'] for r in page.records} == {r['number'] for r in expected.values()}
        assert all(r['gid'] is None for r in page.records)
        button(page, '勾选筛选结果').click()
        assert page.one_click_btn.isEnabled() and '2单' in page.one_click_btn.text()
        page.one_click_btn.click()
        assert set(result_rows) == expected.keys()

    def payment_page(page):
        wait(lambda: getattr(page, 'applied_request', None) is not None and bool(page.records))
        assert page.status.currentText() == '待处理'
        found = {r['warehouse']: (i, r) for i, r in enumerate(page.records)}
        assert set(found) == expected.keys()
        for wh, (i, row) in found.items():
            assert row['number'] == expected[wh]['number'] and row['status'] == '待付款'
            assert page.table.cellWidget(i, 2).text() == '核对聚水潭付款资料'
        snapshot(page, '付款待办')
        checks.append('默认付款核对待办可见两仓原单')

    def enter(label, stage, handle):
        errors = []
        entered = False
        timer = QTimer(win)

        def process(dialog):
            try:
                handle(dialog.panel)
            except BaseException:
                errors.append(traceback.format_exc())
            finally:
                dialog.reject()

        def tick():
            nonlocal entered
            dialog = app.activeModalWidget()
            try:
                if isinstance(dialog, QMessageBox):
                    errors.append(dialog.text())
                    dialog.reject()
                elif isinstance(dialog, StageDialog) and not entered:
                    assert dialog.stage == stage
                    entered = True
                    QTimer.singleShot(0, lambda: process(dialog))
                elif dialog and dialog.windowTitle() == '确认一键分配下单':
                    rows = dialog.findChild(QTableView).model().rows
                    assert len(rows) == 2 and {r[2] for r in rows} == {r['number'] for r in expected.values()}
                    assert all('刷新本仓库存后分配并导入' == r[4] for r in rows)
                    assert not port.calls
                    snapshot(dialog, '一次确认')
                    button(dialog, '确认本次作业').click()
                    checks.append('只确认一次，提交范围是勾选的两张新仓单')
                elif dialog and dialog.windowTitle() == '一键作业结果':
                    rows = dialog.findChild(QTableView).model().rows
                    assert len(rows) == 2 and all(r[4] == '待人工确认付款' for r in rows), rows
                    result_rows.update((r[1], r) for r in rows)
                    snapshot(dialog, '双仓结果')
                    button(dialog, '关闭').click()
            except BaseException:
                errors.append(traceback.format_exc())
                if dialog:
                    dialog.reject()

        timer.timeout.connect(tick)
        timer.start(25)
        try:
            target = button(win.followup, label, direct=True)
            assert target.isEnabled(), label
            target.click()
        finally:
            timer.stop()
        assert entered and not errors, errors

    def evidence():
        keys = payment_jobs.waiting_review(path)
        assert len(keys) == 2
        _, candidates = payment.prepare_review(path, keys)
        items = []
        with closing(wr.read_connection(path)) as db:
            for candidate in candidates:
                head = candidate['head']
                wh = head['warehouse']
                expect = expected[wh]
                saved = tasks.read_warehouse_order(db, head)
                generation, _ = generations.read_generation(db, saved['gid'])
                assert generation['head']['number'] == expect['number'] and candidate['head']['number'] == expect['number']
                batch = batches.context(db, generation['batch_id'])
                adopted = batch['stocks'][wh]['catalog']
                assert adopted['warehouse'] == wh and adopted['revision'] != expect['old_revision']
                assert adopted['items'][expect['sku']]['可用数'] == expect['stock']
                row, = generation['rows']
                count = str(expect['stock'])
                assert (row['sku'], row['demand'], row['stock'], row['before'], row['quantity']) == (expect['sku'], '40', count, count, count)
                assert saved['stage'] == 'IMPORTED' and saved['platform_status'] == 'awaiting'
                assert saved['internal'] and candidate['internal'] == saved['internal']
                if result_rows:
                    assert result_rows[wh][3] == saved['internal']
                state = sources.current(db, saved['online'])
                assert len(sources.history(db, saved['online'])) == 1 and state['state'] == 'READY'
                detail, = candidate['rows']
                assert candidate['source'] == '聚水潭原订单明细' and candidate['quantity'] == count
                assert (detail['sku'], detail['quantity'], Decimal(detail['price']), Decimal(detail['amount'])) == (
                    expect['sku'], count, Decimal(2), Decimal(count)*2)
                assert lifecycle.current(db, head, 'payment') is None
                items.append(dict(warehouse=wh, number=head['number'], sku=expect['sku'], old_stock=expect['old_stock'],
                                  stock=expect['stock'], before=row['before'], demand=row['demand'], quantity=row['quantity'],
                                  batch=batch['batch_id'], stock_revision=adopted['revision'], stock_sha=adopted['source_sha256'],
                                  online=saved['online'], internal=saved['internal'], platform_status='已付款待审核',
                                  detail_count=len(candidate['rows']), source=candidate['source'], amount=candidate['totals']['payable'],
                                  payment_candidate=state['digest'], payment_confirmed=False, state='待人工确认付款'))
        assert len({r['internal'] for r in items}) == 2 and len({r['batch'] for r in items}) == 1
        return sorted(items, key=lambda r: r['warehouse'])

    import jst_fulfillment_jobs as payment_jobs

    def run():
        report = {'passed': False, 'phase': phase, 'frozen_exe': bool(getattr(sys, 'frozen', False)),
                  'synthetic': True, 'live_platform': False, 'build': diagnostic(), 'calls': port.calls, 'checks': checks}
        original = catalog_session_ui.manager
        try:
            catalog_session_ui.manager = lambda _: SimpleNamespace(session=port)
            button(win, '订单跟踪').click()
            win.followup.start.setText(spec['day'])
            win.followup.end.setText(spec['day'])
            if phase == 'exercise':
                client = Client(out/'packaged-component-probe')
                try:
                    client.start()
                    assert client.request('order.capabilities')['payment_detail'] is True
                    report['packaged_component_started'] = True
                finally:
                    assert client.close()
                enter('聚水潭导入单', 'jst', allocate_page)
                refreshes = [call[1][0] for call in port.calls if call[0] == 'sync.start']
                assert sorted(refreshes) == sorted(expected)
                for wh in expected:
                    actions = [call[0] for call in port.calls if call[0].startswith('order.') and call[1] == wh]
                    assert actions == ['order.preview', 'order.stage_import', 'order.import', 'order.lookup', 'order.payment_detail']
                checks.append('每仓刷新一次，预览后只导入一次，再按原单取明细')
            enter('进入付款核对', 'payment', payment_page)
            report['orders'] = evidence()
            checks.append('核对冻结库存、分配明细、内部单号、付款候选及原业务付款状态')
            report['passed'] = True
        except BaseException:
            report['error'] = traceback.format_exc()
        finally:
            catalog_session_ui.manager = original

        with (out/(phase+'-report.json')).open('x', encoding='utf-8') as report_file:
            json.dump(report, report_file, ensure_ascii=False, indent=2)
        win.job_dirty = False
        win._discard_close = True
        if report['passed']:
            win.close()
        else:
            app.exit(1)

    QTimer.singleShot(0, run)
