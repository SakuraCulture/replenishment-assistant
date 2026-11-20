"""Application-owned, opt-in polling. No payment, import or audit commands."""
import sqlite3
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QWidget, QHBoxLayout

from ui_tasks import Worker
import jst_fulfillment_jobs as jobs
import jst_fulfillment as service


class Runner(QObject):
    changed = Signal()

    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.worker = None
        self.legacy_worker = False
        self.stopping = False
        self.blocked = False
        self.close_requested = False
        self.pending = []
        self.message = '实配自动检查未开启'
        self.timer = QTimer(self)
        self.timer.setInterval(30000)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        QTimer.singleShot(0, self.refresh_pending)

    def refresh_pending(self):
        try:
            self.pending = jobs.waiting_review(self.host.work_repository.path)
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.pending = []
            self.message = '实配待核对清单暂不可读：' + str(exc)
            self.changed.emit()
            return False
        self.changed.emit()
        return True

    def tick(self):
        h = self.host
        if not self.worker and not self.stopping and not getattr(h, '_data_location_preparing', False):
            if not self.refresh_pending():
                return
        if (self.worker or self.stopping or self.blocked or getattr(h, 'closing', False)
                or getattr(h, '_data_location_preparing', False) or getattr(h, 'worker', None)
                or QApplication.activeModalWidget()):
            return
        try:
            audited = jobs.due_audited(h.work_repository.path)
            legacy = jobs.enabled(h.work_repository.path)
            if not audited and not legacy:
                if self.message != '实配自动检查未开启':
                    self.status('实配自动检查未开启')
                return
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.status('本地实配清单暂不可读，下次自动检查时重试：' + str(exc))
            return
        from catalog_session_ui import manager
        owner = manager(h).session
        # An idle timer does not start a browser/login/permission handshake.
        if owner.lease is not None or not owner.ready or not owner.client or not owner.client.running:
            self.status('等待已核验聚水潭连接空闲，未发起新登录')
            return
        if self.message == '实配自动检查未开启':
            self.status('实配自动检查已开启，等待原单配货')
        try:
            keys = audited or jobs.due(h.work_repository.path)
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.status('本地实配清单暂不可读，下次自动检查时重试：' + str(exc))
            return
        if not keys:
            return
        self.legacy_worker = not audited
        reader = service.run_background if self.legacy_worker else service.retry_audited
        worker = Worker(reader, (h.work_repository.path, keys, owner), self)
        self.worker = worker
        worker.status.connect(self.status)
        worker.success.connect(self.ready)
        worker.failed.connect(self.failed)
        worker.cancelled.connect(lambda: self.status('已停止实配检查，原单与付款保持'))
        worker.finished.connect(self.finished)
        worker.start()

    def status(self, message):
        self.message = message
        self.changed.emit()

    def configured(self, enabled):
        self.blocked = False
        if not enabled and self.worker and self.legacy_worker:
            self.worker.cancel.set()
        self.status('实配自动检查已开启，等待原单配货' if enabled else '实配自动检查未开启')

    def ready(self, rows):
        count = sum(row['state'] == 'READY' for row in rows)
        self.message = f'付款来源检查 {len(rows)} 单，{count} 单待人工确认付款'
        self.refresh_pending()
        if count and hasattr(self.host, 'status'):
            self.host.status.setText(self.message + '；可直接打开已回流资料。')

    def failed(self, text):
        self.blocked = True
        self.message = '实配检查已暂停，请从原下单任务核对：' + text
        self.changed.emit()

    def open_review(self):
        if self.worker or self.stopping or QApplication.activeModalWidget():
            return
        self.refresh_pending()
        if not self.pending:
            return
        from jst_fulfillment_ui import review
        from lifecycle_ui import warn
        try:
            review(self.host, self.host, list(self.pending))
        except Exception as exc:
            warn(self.host, str(exc))
        finally:
            self.refresh_pending()
            if hasattr(self.host, 'followup'):
                self.host.followup.query()

    def finished(self):
        worker, self.worker = self.worker, None
        worker.deleteLater()
        self.changed.emit()
        if self.close_requested:
            QTimer.singleShot(0, self.host.close)

    def prepare_close(self):
        self.stopping = True
        self.close_requested = True
        self.timer.stop()
        if self.worker:
            self.worker.cancel.set()
            self.host.status.setText('等待当前实配读取安全结束，未确认付款…')
            return False
        return True

    def resume_after_cancelled_close(self):
        self.stopping = self.close_requested = False
        self.timer.start()


def install(host, home_layout):
    """Persistent entry points: collection runs without entering the payment scene."""
    from ui_controls import button, label
    runner = Runner(host)
    for layout in (home_layout, host.followup.layout()):
        panel = QWidget(host)
        row = QHBoxLayout(panel)
        row.setContentsMargins(0, 0, 0, 0)
        note = label('', 'muted')
        action = button('查看已回流实配', runner.open_review)
        row.addWidget(note, 1)
        row.addWidget(action)
        layout.insertWidget(1, panel)
        def refresh(n=note, b=action):
            count = len(runner.pending)
            n.setText(f'{count} 单实配已回流，等待人工确认付款' if count else runner.message)
            b.setText(f'核对已回流实配（{count}）')
            b.setEnabled(bool(count) and runner.worker is None and not runner.stopping)
        runner.changed.connect(refresh)
        refresh()
    return runner
