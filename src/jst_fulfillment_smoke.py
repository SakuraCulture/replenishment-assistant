"""Exercise background return and the payment review in a synthetic application directory."""
from contextlib import closing
import json
from pathlib import Path
import sys
import time
import traceback
from types import SimpleNamespace

from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox, QPushButton


def start_smoke(app, win, args):
    root = Path(args[0]).resolve()
    reopen = 'reopen' in args[1:]
    runner = win.jst_fulfillment_runner
    runner.timer.stop()

    def run():
        import catalog_session_ui
        import jst_fulfillment_jobs as jobs
        from jst_auto_order_store import read
        from lifecycle_ui import Review
        from order_lifecycle import history, snapshot
        from version_info import diagnostic
        from work_repository import read_connection

        path = win.work_repository.path.resolve()
        timer = QTimer(win)
        messages, calls = [], []
        try:
            input_data = json.loads((root / 'synthetic.json').read_text('utf-8'))
            assert input_data['synthetic'] is True and input_data['live_platform'] is False
            assert path == root / 'settings/workbench.sqlite3'
            online = input_data['online']
            native = input_data['receipt']
            native['path'] = str(root / '合成出库明细.xlsx')
            with closing(read_connection(path)) as db:
                head = read(db, online)['payload']['head']

            def paid():
                return snapshot(path, head)['present']['payment']

            if not reopen:
                assert not paid() and not jobs.waiting_review(path) and not jobs.enabled(path)

                class Lease:
                    def start(self):
                        return True

                    def close(self, timeout):
                        return True

                    def request(self, action, *, spec, **kwargs):
                        assert action == 'order.allocation' and spec['online'] == online
                        assert kwargs['retained_only'] is True
                        calls.append(action)
                        return native

                owner = SimpleNamespace(lease=None, ready=True, client=SimpleNamespace(running=True), acquire=Lease)
                manager = catalog_session_ui.manager
                catalog_session_ui.manager = lambda _: SimpleNamespace(session=owner)
                try:
                    jobs.configure(path, True)
                    runner.configured(True)
                    runner.tick()
                    deadline = time.monotonic() + 20
                    while runner.worker and time.monotonic() < deadline:
                        QTest.qWait(20)
                    assert runner.worker is None and calls == ['order.allocation'], runner.message
                finally:
                    catalog_session_ui.manager = manager
                assert jobs.waiting_review(path) == [online] and not paid()

                approve = False

                def choose():
                    dialog = app.activeModalWidget()
                    if isinstance(dialog, QMessageBox):
                        messages.append(dialog.text())
                        dialog.reject()
                    if not isinstance(dialog, Review):
                        return
                    try:
                        assert not dialog.save.isEnabled() and not paid()
                        if approve:
                            dialog.grab().save(str(root / '实配待人工确认.png'))
                            dialog.complete.setChecked(True)
                            assert dialog.save.isEnabled()
                            dialog.save.click()
                        else:
                            dialog.reject()
                    except Exception:
                        messages.append(traceback.format_exc())
                        dialog.reject()

                timer.setInterval(20)
                timer.timeout.connect(choose)
                control = next(b for b in win.findChildren(QPushButton) if b.text() == '核对已回流实配（1）')
                timer.start()
                control.click()
                assert not paid() and jobs.waiting_review(path) == [online]
                approve = True
                control.click()
                timer.stop()
                assert not messages, messages

            assert paid() and jobs.enabled(path)
            assert not jobs.waiting_review(path) and not jobs.due(path)
            versions = history(path, head, 'payment')
            assert len(versions) == 1
            with closing(read_connection(path)) as db:
                assert db.execute('PRAGMA quick_check').fetchone() == ('ok',)
                assert db.execute('SELECT count(*) FROM jst_fulfillment_sources').fetchone() == (1,)
            result = {'passed': True, 'online': online, 'synthetic_reads': len(calls),
                      'payment_versions': len(versions), 'frozen_exe': bool(getattr(sys, 'frozen', False)),
                      **diagnostic()}
        except Exception:
            result = {'passed': False, 'error': traceback.format_exc(), 'messages': messages}
        finally:
            timer.stop()
        report = root / ('reopen.json' if reopen else 'review.json')
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
        if result['passed']:
            win._discard_close = True
            win.close()
        else:
            app.exit(1)

    QTimer.singleShot(80, run)
