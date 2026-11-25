"""Exercise the real Qt export buttons against an explicitly isolated database."""
import json
import os
from pathlib import Path
import sys
import time
import traceback
from unittest.mock import patch

from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QInputDialog, QLabel, QMessageBox, QPushButton


def press(widget, title):
    buttons = [b for b in widget.findChildren(QPushButton) if b.isVisible() and b.text() == title]
    assert len(buttons) == 1 and buttons[0].isEnabled(), title
    QTest.mouseClick(buttons[0], Qt.MouseButton.LeftButton)


def wait(predicate, timeout=90):
    until = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= until:
            raise TimeoutError('等待实际界面结果超时')
        QTest.qWait(25)


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), 'utf-8')


def isolated(win, root):
    assert Path(os.environ['REPLENISHMENT_DATA_DIR']).resolve() == root/'settings'
    assert win.work_repository.path.resolve() == root/'settings/workbench.sqlite3'
    marker = json.loads((root/'synthetic.json').read_text('utf-8'))
    assert (marker.get('synthetic') or marker.get('isolated')) and marker.get('live_platform') is False
    win.auto_check.stop()
    win.scheduled.timer.stop()
    win.jst_generator.stopped = True
    win.jst_generator.timer.stop()
    win.aoxiang_workflows.stopping = True
    win.aoxiang_workflows.timer.stop()


def start_smoke(app, win, args):
    root, mode = Path(args[0]).resolve(), args[1]
    isolated(win, root)

    def exercise():
        from lifecycle_home_ui import StageDialog
        from business_ui import Task
        folder = root/'export'
        destination = str(folder)
        expected = json.loads((root/'fixture.json').read_text('utf-8'))
        count = len(expected['heads'])
        files_expected = count if mode == 'signed' else len({h['warehouse'] for h in expected['heads']})
        began, recorded = None, None
        worker_results = []
        errors = []
        handled = set()

        def start(value):
            nonlocal began
            if began is None:
                began = time.time()
                write(root/'start.json', {'time': began, 'source': value, 'mode': mode, 'orders': count})

        def directory(*args, **kwargs):
            start('confirmed-actual-qt-scope-and-output-directory')
            return destination

        def stage_export(dialog):
            try:
                assert len(dialog.panel.checked) == count
                press(dialog, '导出门店回传单' if mode=='return' else '导出运营签发单')
            except Exception:
                errors.append(traceback.format_exc())
            finally:
                dialog.reject()

        def tick():
            nonlocal recorded
            try:
                active = app.activeModalWidget()
                if active is None or active in handled:
                    return
                title = active.windowTitle()
                if isinstance(active, StageDialog) and active.panel.table.isEnabled():
                    handled.add(active)
                    # Start outside this timer's handler so its next tick can
                    # drive the actual nested export dialogs.
                    QTimer.singleShot(0, active, lambda dialog=active:stage_export(dialog))
                elif isinstance(active, QInputDialog):
                    handled.add(active)
                    assert title=='选择导出方式'
                    active.setTextValue('每仓一表多订单')
                    active.accept()
                elif isinstance(active, Task):
                    handled.add(active)
                    active.worker.success.connect(worker_results.append)
                    active.worker.failed.connect(lambda error:errors.append(str(error)))
                elif isinstance(active, QMessageBox):
                    handled.add(active)
                    if title in ('核对导出范围', '导出签发单'):
                        assert all(h['number'] in active.text() for h in expected['heads'])
                        if mode=='signed':
                            start('confirmed-actual-qt-signing-scope')
                        active.button(QMessageBox.StandardButton.Yes).click()
                    else:
                        errors.append(active.text() + '\n' + active.informativeText())
                        active.accept()
                elif title in ('签发单已导出', '导出结果'):
                    handled.add(active)
                    captions = [label.text() for label in active.findChildren(QLabel)]
                    assert f'已生成 {files_expected} 个文件' in captions
                    recorded = time.time()
                    active.grab().save(str(root/'result.png'))
                    active.accept()
            except Exception:
                errors.append(traceback.format_exc())
                if active is not None:
                    active.reject()

        timer = QTimer(win)
        timer.setInterval(20)
        timer.timeout.connect(tick)
        try:
            press(win, '订单跟踪')
            press(win.followup, '全部未完成（不限日期）')
            wait(lambda:win.followup.table.isEnabled() and len(win.followup.records)==count, 150)
            press(win.followup, '勾选本页')
            assert len(win.followup.checked)==count
            assert list(h['number'] for h in win.followup.checked.values())==sorted(h['number'] for h in expected['heads'])
            timer.start()
            with patch('export_locations.directory', directory):
                if mode=='signed':
                    press(win.followup, '导出勾选签发单')
                else:
                    QTest.mouseClick(win.followup.cards['待回传' if mode=='return' else '待运营签发'],Qt.MouseButton.LeftButton)
            assert began is not None and recorded and not errors, errors
            paths = worker_results[-1]
            if isinstance(paths, dict):
                assert not paths.get('error') and not paths.get('cancelled'), paths
                paths = paths['files']
            assert len(paths)==files_expected and all(Path(path).is_file() for path in paths)
            write(root/'result.json', {'time': recorded, 'state': 'COMPLETE', 'files': list(paths), 'orders': count,
                                       'frozen_exe': bool(getattr(sys,'frozen',False)), 'directory_picker_isolated': mode!='signed'})
        except Exception:
            write(root/'result.json', {'time': time.time(), 'state': 'FAILED', 'traceback': traceback.format_exc(), 'errors': errors})
        finally:
            timer.stop()
            win.job_dirty = False
            win._discard_close = True
            win.close()
    QTimer.singleShot(100, exercise)
