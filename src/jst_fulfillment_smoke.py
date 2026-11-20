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
        original_manager = catalog_session_ui.manager
        original_due_audited = jobs.due_audited
        try:
            input_data = json.loads((root / 'synthetic.json').read_text('utf-8'))
            assert input_data['synthetic'] is True and input_data['live_platform'] is False
            assert path == root / 'settings/workbench.sqlite3'
            online = input_data['online']
            native = input_data['receipt']
            audited = native.get('native_status') == '已客审待财审'
            action_name = 'order.payment_detail' if audited else 'order.allocation'
            if not audited:
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
                        assert action == action_name and spec['online'] == online
                        assert spec['internal'] == native['internal'] and spec['binding'] == native['binding']
                        calls.append(kwargs['retained_only'])
                        return native

                owner = SimpleNamespace(lease=None, ready=True, client=SimpleNamespace(running=True), acquire=Lease)
                catalog_session_ui.manager = lambda _: SimpleNamespace(session=owner)
                if not audited:
                    # Exercise opt-in historical collection with the historical fixture.
                    jobs.due_audited = lambda _: []
                    jobs.configure(path, True)
                    runner.configured(True)
                runner.tick()
                deadline = time.monotonic() + 20
                while runner.worker and time.monotonic() < deadline:
                    QTest.qWait(20)
                assert runner.worker is None and calls == [True], runner.message
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
                assert calls == ([True, False] if audited else [True])

            assert paid() and jobs.enabled(path) is (not audited)
            assert not jobs.waiting_review(path) and not jobs.due(path)
            versions = history(path, head, 'payment')
            assert len(versions) == 1
            with closing(read_connection(path)) as db:
                assert db.execute('PRAGMA quick_check').fetchone() == ('ok',)
                assert db.execute('SELECT count(*) FROM jst_fulfillment_sources').fetchone() == (1,)
            result = {'passed': True, 'online': online, 'synthetic_reads': len(calls), 'audited_source': audited,
                      'payment_versions': len(versions), 'frozen_exe': bool(getattr(sys, 'frozen', False)),
                      **diagnostic()}
            if audited and not reopen:
                result['single_order_xls'] = _large_xls(root, online)
        except Exception:
            result = {'passed': False, 'error': traceback.format_exc(), 'messages': messages}
        finally:
            timer.stop()
            catalog_session_ui.manager = original_manager
            jobs.due_audited = original_due_audited
        report = root / ('reopen.json' if reopen else 'review.json')
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
        if result['passed']:
            win._discard_close = True
            win.close()
        else:
            app.exit(1)

    QTimer.singleShot(80, run)


def _large_xls(root, online):
    from hashlib import sha256
    import xlrd
    import jst_auto_order_template as template

    contacts = dict(zip(template.CONTACT,
                        ('000123','合成收货人','00000000000','山东省','济南市','历下区','合成测试路')))
    lines, stats = template.order_lines([
        {'sku': f'@{i:019}', 'quantity': i % 3 + 1, 'price': '2.75'} for i in range(2000)])
    assert stats['sku_cell_chars'] > template.MAX_CELL
    raw, _ = template.make_file(contacts, online, lines, stats, import_format=template.SINGLE)
    sheet = xlrd.open_workbook(file_contents=raw).sheet_by_name(template.SHEET)
    assert (sheet.nrows, sheet.ncols) == (2001, 39)
    for i, (sku, qty, _) in enumerate(lines, 1):
        assert [sheet.cell_value(i, col) for col in (0, 20, 23, 24)] == [online, sku, int(qty), '']
    (root / '合成两千款整单.xls').write_bytes(raw)
    return {**stats, 'bytes': len(raw), 'sha256': sha256(raw).hexdigest()}
