"""Exercise the payment work page and local AX preview using an isolated fixture."""

import json
import os
import sys
import time
import traceback
from decimal import Decimal
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton


def start_smoke(app, win, args):
    from aoxiang_client import ProcessClient
    from aoxiang_preview_ui import PreviewScope
    from aoxiang_workflow_ui import QueueDialog
    from lifecycle_batch_ui import BatchImport
    from lifecycle_home_ui import StageDialog
    from daily_batch import PURCHASE_FILTER
    from version_info import diagnostic

    out, mode = Path(args[0]).resolve(), args[1]
    marker = json.loads((out/'synthetic.json').read_text('utf-8'))
    assert marker['synthetic'] is True and marker['live_platform'] is False
    assert Path(os.environ['REPLENISHMENT_DATA_DIR']).resolve() == out/'settings'
    assert win.work_repository.path.resolve() == out/'settings/workbench.sqlite3'
    fixture = json.loads((out/'fixture.json').read_text('utf-8'))
    win.auto_check.stop()
    win.jst_generator.stopped = True
    win.jst_generator.timer.stop()
    manager = win.aoxiang_workflows
    manager.timer.stop()
    calls, checks, previews, images = [], [], [], []
    state = {'phase': '', 'error': None, 'confirmation': False}
    window = None

    class OfflineClient(ProcessClient):
        def request(self, action, **kw):
            calls.append(action)
            if action == 'setup.check_login':
                return {'id': 'isolated-login', 'state': 'VERIFIED'}
            if action == 'template.start':
                return {'id': 'isolated-template', 'state': 'RUNNING'}
            if action == 'template.status':
                return {'id': 'isolated-template', 'state': 'READY', 'receipt': fixture['template']}
            assert action in {'configuration','ping','shutdown','session.health','session.release',
                              'purchase.list','purchase.status','purchase.reserve'}, action
            return super().request(action, **kw)

    manager.client_factory = OfflineClient

    def shot(widget, name):
        filename = mode+'-'+name+'.png'
        assert widget.grab().save(str(out/filename)), filename
        images.append(filename)

    def wait(predicate, timeout=55):
        deadline = time.monotonic() + timeout
        while not predicate() and state['error'] is None and time.monotonic() < deadline:
            QTest.qWait(20)
        assert state['error'] is None, state['error']
        assert predicate(), '超时 '+state['phase']

    def check_job(job, numbers):
        assert [item['key'] for item in job['items']] == numbers, job
        for item in job['items']:
            assert item['state'] == 'PREPARED'
            package = json.loads(Path(item['package']['package_file']).read_text('utf-8'))
            amount = '15.00' if item['key'] == fixture['paid'] else '8.00'
            qty = '6' if item['key'] == fixture['paid'] else '4'
            assert package['supplierCode'] == '2078246'
            sku, = package['rows']
            assert sku['sku'] == '0001' and Decimal(sku['quantity']) == Decimal(qty)
            assert Decimal(sku['amount']) == Decimal(amount)
            assert package['basis']['paymentVersion'] == item['expected_payment']['version'] == 1
            assert package['basis']['paymentDigest'] == item['expected_payment']['digest']
            previews.append({'仓单':item['key'], 'SKU':sku['sku'], '数量':qty,'金额':amount})

    def click_original(dialog, button):
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        dialog.reject()

    def confirm_then_cancel(dialog):
        try:
            QTest.mouseClick(dialog.select_all_btn, Qt.MouseButton.LeftButton)
            assert dialog.upload_btn.isEnabled()
            QTest.mouseClick(dialog.upload_btn, Qt.MouseButton.LeftButton)
            assert state['confirmation']
            state['confirmation'] = False
        except Exception:
            state['error'] = traceback.format_exc()
        finally:
            dialog.reject()

    def handle_modal():
        modal = QApplication.activeModalWidget()
        if state['error']:
            if modal and not getattr(modal, 'busy', False):
                modal.reject()
            return
        try:
            if isinstance(modal, BatchImport) and not modal.busy:
                if not modal.paths:
                    modal.add_paths([str(out/'payment.xlsx')])
                elif modal.pages.currentIndex() == 0:
                    assert modal.read_ready and not modal.issues, modal.issues
                    QTest.mouseClick(modal.preview_btn, Qt.MouseButton.LeftButton)
                else:
                    modal.review.complete.setChecked(True)
                    modal.review.extra_ack.setChecked(True)
                    assert modal.review.save.isEnabled()
                    QTest.mouseClick(modal.review.save, Qt.MouseButton.LeftButton)
            elif isinstance(modal, PreviewScope) and not modal.busy and not getattr(modal, '_qa_clicked', False):
                modal._qa_clicked = True
                if state['phase'] == 'import':
                    assert modal.saved and len(modal.ready_heads) == 1
                    assert modal.ready_heads[0]['number'] == fixture['paid']
                    assert modal.table.rowCount() == 2
                    assert '尚未确认' in modal.table.item(1,4).text(), modal.table.item(1,4).text()
                    assert modal.prepare_btn.text() == '继续生成翱象采购预览（1 单）'
                    shot(modal, '付款保存一键继续')
                elif state['phase'] == 'bulk':
                    assert [h['number'] for h in modal.ready_heads] == fixture['bulk_selected']
                    shot(modal, '跨页所选范围')
                else:
                    assert not modal.ready_heads and modal.table.rowCount() == 1
                    button = modal.table.cellWidget(0,5)
                    assert button and button.text() == '打开原批次'
                    shot(modal, '继续原批次')
                    QTimer.singleShot(0, lambda d=modal,b=button: click_original(d,b))
                    return
                QTest.mouseClick(modal.prepare_btn, Qt.MouseButton.LeftButton)
            elif isinstance(modal, QueueDialog):
                current = manager.store.get(modal.id)
                if current['state'] in ('COMPLETED_WITH_ERRORS','PAUSED'):
                    raise AssertionError(current)
                if current['state'] != 'READY' or manager.worker or getattr(modal, '_qa_clicked', False):
                    return
                modal._qa_clicked = True
                modal.refresh()
                if state['phase'] in ('original', 'reopen'):
                    assert modal.id == (out/'original-batch.txt').read_text('utf-8')
                    assert len(current['items']) == 1 and current['items'][0]['key'] == fixture['paid']
                else:
                    check_job(current, [fixture['paid']] if state['phase'] == 'import' else fixture['bulk_selected'])
                    if state['phase'] == 'import':
                        (out/'original-batch.txt').write_text(modal.id,'utf-8')
                    shot(modal, '预览-'+state['phase'])
                    QTimer.singleShot(0, lambda d=modal: confirm_then_cancel(d))
                    return
                modal.reject()
            elif isinstance(modal, QMessageBox):
                assert modal.windowTitle() == '确认批量上传采购', (modal.windowTitle(),modal.text())
                assert modal.defaultButton() == modal.button(QMessageBox.StandardButton.No)
                assert all(n in modal.text() for n in ([fixture['paid']] if state['phase']=='import' else fixture['bulk_selected']))
                shot(modal, '上传确认-'+state['phase'])
                state['confirmation'] = True
                modal.button(QMessageBox.StandardButton.No).click()
        except Exception:
            state['error'] = traceback.format_exc()
            if modal:
                modal.reject()

    timer = QTimer(win)
    timer.timeout.connect(handle_modal)
    timer.start(30)

    def exercise():
        nonlocal window
        error = None
        try:
            win.show_section(3)
            window = StageDialog(win.followup, 'payment')
            window.show()
            panel = window.panel
            wait(lambda: getattr(panel, 'applied_request', None) is not None)
            if mode != 'reopen':
                state['phase'] = 'import'
                button, = panel.primary_actions
                assert button.text() == '导入门店付款单'
                QTest.mouseClick(button, Qt.MouseButton.LeftButton)
                wait(lambda: (out/'original-batch.txt').exists())
                checks.append('实际付款导入与保存后，一次点击用有效河北仓单生成正式采购预览；同店山东待核留在原状态')
                state['phase'] = 'bulk'
                panel.status.setCurrentText(PURCHASE_FILTER)
                wait(lambda: panel.purchase_count == 190)
                QTest.mouseClick(panel.purchase_selection_btn, Qt.MouseButton.LeftButton)
                assert len(panel.checked) == 190 and panel.table.rowCount() == 50
                shot(window, '跨四页勾选190单')
                panel.turn(3)
                wait(lambda: panel.page == 3 and panel.purchase_count == 190)
                assert panel.table.rowCount() == 40
                panel.table.item(0,0).setCheckState(Qt.CheckState.Unchecked)
                assert len(panel.checked) == 189
                panel.select_page(False)
                panel.table.item(39,0).setCheckState(Qt.CheckState.Checked)
                panel.turn(-3)
                wait(lambda: panel.page == 0 and panel.purchase_count == 190)
                panel.table.item(0,0).setCheckState(Qt.CheckState.Checked)
                assert len(panel.checked) == 2
                QTest.mouseClick(next(b for b in panel.findChildren(QPushButton)
                    if b.text() == '勾选单批量生成翱象采购预览'), Qt.MouseButton.LeftButton)
                assert state['error'] is None, state['error']
                assert len(previews) == 3
                checks.append('同一筛选跨四页勾选190单，可取消个别仓单，按最终跨页勾选生成且顺序一致')
                checks.append('三个实际采购文件逐单核对SKU、数量、金额、供应商、付款版本和摘要；上传确认默认取消')
            state['phase'] = 'reopen' if mode == 'reopen' else 'original'
            panel.search.setText(fixture['paid'])
            panel.status.setCurrentText('本环节全部')
            panel.query()
            wait(lambda: len(panel.records)==1 and panel.records[0]['number']==fixture['paid'])
            panel.table.item(0,0).setCheckState(Qt.CheckState.Checked)
            before = manager.store.list()
            panel.aoxiang_prepare()
            assert state['error'] is None, state['error']
            assert manager.store.list() == before
            assert not any(j['kind']=='upload' for j in before)
            checks.append('从付款核对按原仓单打开已有采购批次，队列无重复预览和上传任务')
            assert ('purchase.reserve' in calls) == (mode != 'reopen')
        except Exception:
            error = traceback.format_exc()
        finally:
            timer.stop()
            if window:
                window.reject()
                window.deleteLater()
            output = {'passed':error is None,'error':error,'checks':checks,'screenshots':images,
                      'preview_rows':previews,'component_actions':calls,'live_platform':False,
                      'frozen_exe':bool(getattr(sys,'frozen',False)),'version':diagnostic()}
            (out/(mode+'-report.json')).write_text(json.dumps(output,ensure_ascii=False,indent=2),'utf-8')
            win._discard_close = True
            win.close()
            QTimer.singleShot(150, lambda: app.exit(1 if error else 0))

    QTimer.singleShot(250, exercise)
