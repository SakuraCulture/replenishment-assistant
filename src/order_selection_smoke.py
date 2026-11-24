"""Native selection export and format guard on marked, isolated data only."""
import json
import os
from pathlib import Path
import time
import traceback
from unittest.mock import patch

from PySide6.QtCore import QDate, QTimer, Qt
from PySide6.QtWidgets import QApplication, QDateEdit, QDialog, QFileDialog, QMessageBox, QPushButton, QTableWidget


def start_smoke(app, win, args):
    out, mode = Path(args[0]).resolve(), args[1]
    marker = json.loads((out / 'synthetic.json').read_text('utf-8'))
    if marker.get('synthetic') is not True or marker.get('live_platform') is not False or Path(os.environ.get('REPLENISHMENT_DATA_DIR', '')).resolve() != out / 'settings':
        raise ValueError('Only marked, isolated order-export acceptance data is allowed')

    win.auto_check.stop()
    win.jst_generator.stopped = True
    win.jst_generator.timer.stop()
    win.aoxiang_workflows.stopping = True
    win.aoxiang_workflows.timer.stop()
    win.browser_status.paused = True
    win.browser_status.timer.stop()
    win.browser_status.initial.stop()

    started = time.perf_counter()
    state = {'button': False, 'choose': False, 'preview': None, 'operation_started': None, 'finished': False}

    def close():
        win._discard_close = True
        win.close()

    def record(value):
        (out / 'selection-ui.json').write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

    def finish(box, value):
        state['finished'] = True
        timer.stop()
        record(value)
        if box is not None:
            box.accept()
        dialog = QApplication.activeModalWidget()
        while isinstance(dialog, QDialog):
            dialog.reject()
            other = QApplication.activeModalWidget()
            if other is dialog:
                break
            dialog = other
        QTimer.singleShot(0, win, close)

    def tick():
        box = QApplication.activeModalWidget()
        try:
            if time.perf_counter() - started > 100:
                raise TimeoutError('Isolated selection dialog did not complete')
            if isinstance(box, QMessageBox):
                title = box.windowTitle()
                success = {'by-date': '所选订单迁移包已导出', 'checked': '所选订单迁移包已导出',
                           'full': '迁移包已导出', 'reject-restore': '订单未迁入'}[mode]
                if title != success or (mode == 'reject-restore' and '请使用「合并导入订单」' not in box.text()):
                    raise AssertionError((title, box.text()))
                box.grab().save(str(out / 'result.png'))
                finish(box, {'passed': True, 'mode': mode, 'frozen': bool(getattr(__import__('sys'), 'frozen', False)),
                             'seconds': time.perf_counter() - state['operation_started'],
                             'preview': state['preview'], 'notice': box.text()})
            elif isinstance(box, QDialog) and box.windowTitle() == '核对所选订单迁移范围' and state['preview'] is None:
                table = box.findChild(QTableWidget, 'orderSelectionPreview')
                rows = [[table.item(i, j).text() for j in range(table.columnCount())] for i in range(table.rowCount())]
                observed = {row[2]: row[5] == '原选' for row in rows}
                if observed != marker['expected']:
                    raise AssertionError((observed, marker['expected']))
                state['preview'] = rows
                if not box.grab().save(str(out / 'scope.png')):
                    raise AssertionError('Scope screenshot missing')
                QTimer.singleShot(0, box, box.findChild(QPushButton, 'confirmOrderSelection').click)
            elif isinstance(box, QDialog) and box.windowTitle() == '选择要迁移的订单' and not state['choose']:
                state['choose'] = True
                if mode == 'by-date':
                    box.findChild(QDateEdit, 'orderSelectionDate').setDate(QDate.fromString(marker['business_day'], 'yyyy-MM-dd'))
                    action = box.findChild(QPushButton, 'previewByBusinessDate')
                else:
                    table = box.findChild(QTableWidget, 'orderSelectionTable')
                    for i in range(table.rowCount()):
                        if table.item(i, 3).text() == marker['selected_number']:
                            table.item(i, 0).setCheckState(Qt.CheckState.Checked)
                    action = box.findChild(QPushButton, 'previewSelectedOrders')
                state['operation_started'] = time.perf_counter()
                QTimer.singleShot(0, box, action.click)
            elif isinstance(box, QDialog) and box.windowTitle() == '完整订单迁移与参数导出' and not state['button']:
                state['button'] = True
                if mode in ('by-date', 'checked'):
                    action = box.findChild(QPushButton, 'exportOrderSelection')
                else:
                    text = '导出完整订单迁移包…' if mode == 'full' else '校验并导入完整订单迁移包…'
                    action = next(button for button in box.findChildren(QPushButton) if button.text() == text)
                    state['operation_started'] = time.perf_counter()
                QTimer.singleShot(0, box, action.click)
        except BaseException:
            finish(box, {'passed': False, 'mode': mode, 'error': traceback.format_exc()})

    timer = QTimer(app, interval=25)
    timer.timeout.connect(tick)
    timer.start()
    app._selection_timer = timer

    def begin():
        from order_migration_ui import open_migration
        with patch.object(QFileDialog, 'getSaveFileName', return_value=(str(out / 'orders.zip'), 'Zip')), \
                patch.object(QFileDialog, 'getOpenFileName', return_value=(str(out / 'orders.zip'), 'Zip')):
            open_migration(win, win)

    QTimer.singleShot(220, win, begin)
