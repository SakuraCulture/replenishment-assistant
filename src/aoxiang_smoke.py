"""Explicit isolated EXE smoke; no browser binding, platform query or upload."""
import json
import os
import sys
import time
import traceback
from pathlib import Path
from PySide6.QtCore import QTimer, QEventLoop, Qt
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtTest import QTest


def start_smoke(app, win, args):
    app.setQuitOnLastWindowClosed(False)
    output = Path(args[0]).resolve()
    business = len(args)>1 and args[1]=='business'
    checks, gaps = [], []
    active = {'dialog':None, 'error':None}
    previous = [time.perf_counter()]
    timer = QTimer(win)
    timer.setInterval(30)

    def heartbeat():
        now = time.perf_counter()
        gaps.append(now-previous[0])
        previous[0] = now
        modal = QApplication.activeModalWidget()
        if isinstance(modal, QMessageBox):
            active['error'] = modal.windowTitle()+': '+modal.text()
            modal.reject()

    timer.timeout.connect(heartbeat)
    timer.start()

    def wait(condition):
        loop, poll, began = QEventLoop(), QTimer(), time.monotonic()
        poll.timeout.connect(lambda:loop.quit() if condition() or active['error'] or time.monotonic()-began>45 else None)
        poll.start(10)
        loop.exec()
        poll.stop()
        if active['error'] or not condition():
            raise RuntimeError(active['error'] or 'EXE local operation timeout')

    def execute():
        report = {'passed':False, 'actual_exe':bool(getattr(sys, 'frozen', False)), 'live_platform':False, 'checks':checks}
        try:
            from aoxiang_fetch_ui import FetchDialog
            from aoxiang_workflow_ui import QueueDialog
            from aoxiang_client import ComponentError
            if Path(os.environ.get('REPLENISHMENT_DATA_DIR', '')).resolve() != output/'settings' or win.work_repository.path.resolve() != output/'settings/workbench.sqlite3':
                raise ValueError('Explicit isolated settings required')
            component = output/'settings/aoxiang/component-state'
            if (component/'config/machine.binding.json').exists() or (not business and (component/'config/portable.config.json').exists()):
                raise ValueError('Smoke requires an unconfigured local component; no live binding allowed')
            win._discard_close = True
            win.jst_generator.cancel()
            wait(lambda:win.jst_generator.worker is None)
            from aoxiang_setup_smoke import exercise_setup
            exercise_setup(win,output,wait,active,checks)
            for index in range(0 if business else 2):
                dialog = FetchDialog(win, win.repository.folder, win.job_day)
                active['dialog'] = dialog
                dialog.show()
                wait(lambda:dialog.configuration is not None and dialog.worker is None)
                if dialog.configuration.get('configured'):
                    raise ValueError('Unexpected platform configuration')
                checks.append('真实固定Node进程启动并读取本地未配置状态，第'+str(index+1)+'次')
                values = []
                def protocol(_):
                    try:
                        dialog.client.request('binding.start', purchase_id='offline', binding_request={})
                    except ComponentError as exc:
                        values.append(exc.code)
                    else:
                        raise ValueError('Invalid form unexpectedly accepted')
                    return dialog.client.request('ping')
                dialog.run(protocol, lambda data:values.append(data))
                wait(lambda:dialog.worker is None)
                if len(values) != 2 or values[1]['closing']:
                    raise ValueError('Failed protocol validation/reuse')
                dialog.grab().save(str(output/f'fetch-local-{index+1}.png'))
                dialog.close()
                wait(lambda:dialog._can_close and dialog.worker is None)
                if dialog.client.running:
                    raise ValueError('Component remained alive after close')
                checks.append('字段错误后同进程可用，安全关闭后原状态可重开')
                dialog.deleteLater()
                active['dialog'] = None
            if business:
                from aoxiang_business_smoke import exercise
                exercise(win,output,wait,active,checks)
            from aoxiang_shared_smoke import exercise_shared
            exercise_shared(win,output,wait,active,checks,business)
            queue = QueueDialog(win, win.aoxiang_workflows)
            queue.show()
            QTest.qWait(80)
            if queue.current is not None:
                raise ValueError('Unexpected work item')
            queue.grab().save(str(output/'queue-local.png'))
            queue.close()
            queue.deleteLater()
            checks.append('本地批次队列打开、空状态与关闭；未创建周期计划')
            report.update(passed=True, heartbeat_samples=len(gaps), max_heartbeat_gap=max(gaps, default=0),
                          source_commit=json.loads((Path(sys._MEIPASS)/'build_info.json').read_text(encoding='utf-8'))['source_commit'] if getattr(sys,'frozen',False) else None)
        except Exception:
            report['error'] = traceback.format_exc()
        finally:
            timer.stop()
            dialog = active['dialog']
            if dialog is not None:
                active['error'] = None
                dialog.close()
                wait(lambda:dialog._can_close and dialog.worker is None)
                dialog.deleteLater()
            (output/'smoke.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
            win.job_dirty = False
            win._discard_close = True
            win.close()
            wait(lambda: win.aoxiang_workflows.session.client is None and win.aoxiang_workflows.session_closer is None and win.aoxiang_workflows.owner is None)
            QTimer.singleShot(100, lambda:app.exit(0 if report['passed'] else 1))

    QTimer.singleShot(200, execute)
