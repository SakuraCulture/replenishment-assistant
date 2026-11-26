"""Poll original orders and lock receipt evidence for payments already saved."""
import sqlite3
import time
from datetime import datetime
from PySide6.QtCore import QObject, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import QApplication, QWidget, QHBoxLayout

from ui_tasks import Worker
import jst_fulfillment_jobs as jobs
import jst_fulfillment as service
import jst_fulfillment_round as rounds
from scheduled_replenishment import TZ


class Runner(QObject):
    changed = Signal()

    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.worker = None
        self.legacy_worker = False
        self.stopping = False
        self.blocked = False
        self.timeout_streak = 0
        self.next_read = 0.0
        self.close_requested = False
        self.pending = []
        self.next_at = 0
        self.waiting = False
        self.blocker = None
        self.catalog_manager = None
        self.continue_round = False
        self.message = '等待聚水潭原单付款资料'
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self.tick)
        self.timer.start(0)

    def refresh_pending(self):
        try:
            self.pending = jobs.waiting_review(self.host.work_repository.path)
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.pending = []
            self.message = '付款待核对清单暂不可读：' + str(exc)
            self.changed.emit()
            return False
        self.changed.emit()
        return True

    @Slot()
    def wake(self):
        if self.waiting and not self.stopping and self.worker is None:
            self.waiting = False
            self.timer.start(0)

    @Slot()
    def blocker_finished(self):
        self.blocker = None
        self.wake()

    @Slot()
    def browser_idle(self):
        if self.blocker is None:
            self.wake()

    def wait_for(self, blocker):
        self.waiting = True
        if self.blocker is not blocker:
            self.blocker = blocker
            signal = getattr(blocker, 'finished', None)
            if signal is None:
                signal = blocker.destroyed
            signal.connect(self.blocker_finished, Qt.ConnectionType.SingleShotConnection)
            if hasattr(blocker, 'isFinished') and blocker.isFinished():
                self.blocker_finished()

    def tick(self):
        h = self.host
        if self.worker or self.stopping or getattr(h, 'closing', False) or getattr(h, '_data_location_preparing', False):
            return
        self.waiting = False
        path = h.work_repository.path
        try:
            deadline, entries = rounds.current(path, historical=not self.blocked)
            self.timer.start(max(1, min(rounds.INTERVAL_MS, deadline - rounds.now_ms())))
            if deadline != self.next_at:
                self.next_at = deadline
                self.refresh_pending()
            if not entries:
                return
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.timer.start(5 * 60 * 1000)
            self.status('本地回查计划暂不可读，5 分钟后再检查：' + str(exc))
            return
        if getattr(h, 'worker', None):
            self.wait_for(h.worker)
            return
        from lifecycle_home_ui import StageDialog
        modal = QApplication.activeModalWidget()
        if modal:
            if isinstance(modal, StageDialog) and modal.stage in ('payment', 'receipt'):
                entries = [(key, kind) for key, kind in entries if kind == 'lock']
            else:
                entries = []
            if not entries:
                self.wait_for(modal)
                return
        remaining_backoff = self.next_read - time.monotonic()
        if remaining_backoff > 0:
            self.timer.start(min(self.timer.interval(), int(remaining_backoff * 1000) + 1))
            return
        from catalog_session_ui import manager
        if self.catalog_manager is None:
            self.catalog_manager = manager(h)
            self.catalog_manager.idle.connect(self.browser_idle, Qt.ConnectionType.QueuedConnection)
        owner = self.catalog_manager.session
        if owner.lease is not None or owner.scheduled_waiting or not owner.ready or not owner.client or not owner.client.running:
            self.waiting = True
            self.status('等待已核验聚水潭连接空闲，未发起新登录')
            return
        self.legacy_worker = entries[0][1] == 'historical'
        if self.legacy_worker and self.blocked:
            return
        keys = [key for key, _ in entries]
        worker = Worker(service.read_background_round, (path, keys, owner, deadline, self.legacy_worker), self)
        self.continue_round = False
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
        self.status('历史出库资料检查已开启' if enabled else '等待聚水潭原单付款资料')

    def ready(self, rows):
        self.continue_round = True
        count = sum(row['state'] == 'READY' for row in rows)
        self.message = f'付款来源检查 {len(rows)} 单，{count} 单待人工确认付款'
        if any(row.get('timed_out') for row in rows):
            if any(not row.get('timed_out') for row in rows):self.timeout_streak=0
            self.timeout_streak=min(self.timeout_streak+1,5)
            delay=min(60*2**(self.timeout_streak-1), 900)
            self.next_read=time.monotonic()+delay
            self.message+=f'，查询超时，本轮如有余单将在 {delay} 秒后继续'
        elif rows:
            self.timeout_streak=0
            self.next_read=0.0
        locked = sum(bool(row.get('payment_locked')) for row in rows)
        if locked:
            from lifecycle_home_ui import StageDialog
            self.message += f'，{locked} 单已锁定付款并转待收货'
            modal = QApplication.activeModalWidget()
            if isinstance(modal, StageDialog) and modal.stage in ('payment', 'receipt'):
                modal.panel.query(keep_page=True)
            if hasattr(self.host, 'followup'):
                self.host.followup.query(keep_page=True)
            if hasattr(self.host, 'home_tasks'):
                self.host.home_tasks.refresh()
        self.refresh_pending()
        if count and hasattr(self.host, 'status'):
            self.host.status.setText(self.message + '；可直接打开已回流资料。')

    def failed(self, text):
        if self.legacy_worker:
            self.blocked = True
        self.message = ('历史实配检查已暂停：' if self.legacy_worker else '原单回查暂未完成，下一轮再检查：') + text
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
        elif not self.stopping and (self.continue_round or not self.timer.isActive()):
            self.timer.start(0)

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
        self.timer.start(0)


def install(host, home_layout):
    """Persistent entry points: collection runs without entering the payment scene."""
    from ui_controls import button, label
    runner = Runner(host)
    for layout in (home_layout, host.followup.layout()):
        panel = QWidget(host)
        row = QHBoxLayout(panel)
        row.setContentsMargins(0, 0, 0, 0)
        note = label('', 'muted')
        action = button('核对回读付款资料', runner.open_review)
        row.addWidget(note, 1)
        row.addWidget(action)
        layout.insertWidget(1, panel)
        def refresh(n=note, b=action):
            count = len(runner.pending)
            text = f'{count} 单付款资料已读回，待人工确认付款' if count else runner.message
            if runner.next_at:
                next_check = datetime.fromtimestamp(runner.next_at / 1000, TZ)
                text += f' · 下轮原单回查 {next_check:%m-%d %H:%M}（北京时间，每12小时）'
            n.setText(text)
            b.setText(f'核对回读付款资料（{count}）')
            b.setEnabled(bool(count) and runner.worker is None and not runner.stopping)
        runner.changed.connect(refresh)
        refresh()
    return runner
