"""Non-modal startup checks using existing shared session owners and operation gates."""
import threading
import time
from datetime import datetime
from PySide6.QtCore import QObject, QThread, QTimer, Signal
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QGridLayout
from ui_controls import button
from browser_startup import result, error_result, check_ax, check_jst

NAMES = ('翱象', '山东仓', '河北仓')
TEXT = {'CHECKING': '连接核验中', 'READY': '登录已核验', 'LOGIN_REQUIRED': '需要登录',
        'WRONG_ACCOUNT': '账号不符', 'CONFIG_REQUIRED': '需要配置', 'FAILED': '连接异常',
        'WAITING': '等待原操作', 'DISABLED': '已停用', 'STOPPED': '检查已停止'}


def start_application_connections(host, argv):
    # Existing smoke modes have isolated business settings but no live-browser
    # fixtures. Do not let their new startup side effect open real platforms.
    if any(a.startswith('--') and ('smoke' in a or a == '--business-flow-acceptance') for a in argv):
        return
    startup = getattr(host, 'browser_startup', None)
    if startup:
        startup.start()


class CheckWorker(QThread):
    update = Signal(str, object)

    def __init__(self, kind, names, lease, cancel, release, parent):
        super().__init__(parent)
        self.kind, self.names, self.lease, self.cancel, self.release = kind, names, lease, cancel, release
        self.seen = set()
        self.released = threading.Event()

    def publish(self, name, value):
        if value['state'] != 'CHECKING':
            self.seen.add(name)
        self.update.emit(name, value)

    def run(self):
        try:
            if self.cancel.is_set():return
            if self.kind == 'ax':
                check_ax(self.lease, self.cancel, self.publish)
            else:
                check_jst(self.lease, self.names, self.cancel, self.publish)
        except Exception as exc:
            for name in self.names:
                if name not in self.seen:
                    self.publish(name, result('STOPPED', '已停止连接检查，原配置与任务保持。') if self.cancel.is_set() else error_result(exc))
        finally:
            # Retain the browser, drain only this lease; never force-kill a task.
            try:
                while not self.lease.close(timeout=.3):
                    time.sleep(.15)
            finally:
                self.release()
                self.released.set()


class StartupConnections(QObject):
    changed = Signal()

    def __init__(self, host):
        super().__init__(host)
        self.host = host; self.workers = {}; self.records = {}; self.started = False
        self.closing = False; self.paused=False; self.waiting = set(); self.retry_until = 0
        self.timer = QTimer(self); self.timer.setInterval(15000); self.timer.timeout.connect(self.retry_pending)

    def start(self):
        if self.started:
            return
        self.started = True
        self.retry_until = time.monotonic() + 300
        self.retry()
        self.timer.start()

    def retry(self, names=None):
        if self.closing or self.paused or getattr(self.host,'_data_location_preparing',False) or getattr(self.host, 'closing', False):
            return
        names = list(names or NAMES)
        self.retry_until = max(self.retry_until, time.monotonic() + 300)
        for kind, targets in [('ax', [n for n in names if n == '翱象']), ('jst', [n for n in names if n != '翱象'])]:
            if not targets or kind in self.workers:
                continue
            try:
                if kind == 'ax':
                    manager = self.host.aoxiang_workflows
                    if manager.owner is None:
                        raise RuntimeError('另一应用实例持有翱象连接，请使用原实例或正常退出原实例后重试。')
                    token = manager.gate.claim_background()
                    if token is None:
                        self.defer(targets); continue
                    lease = manager.session.lease(cancelled=token.is_set)
                    release = lambda m=manager, t=token: m.gate.release_background(t)
                else:
                    from catalog_session_ui import manager
                    owner = manager(self.host).session
                    if owner.lease is not None:
                        self.defer(targets); continue
                    token = threading.Event(); lease = owner.acquire(); release = lambda: None
                worker = CheckWorker(kind, targets, lease, token, release, self)
                self.workers[kind] = worker
                for name in targets:
                    self.waiting.discard(name)
                    self.records[name] = result('CHECKING', '应用已启动，正在自动连接并检查登录…')
                worker.update.connect(self.updated); worker.finished.connect(self.finished); worker.start()
            except Exception as exc:
                for name in targets:
                    self.records[name] = error_result(exc)
        self.changed.emit()

    def defer(self, names):
        for name in names:
            self.waiting.add(name); self.records[name] = result('WAITING', '原业务正在使用连接，完成后自动检查；不会打断原任务。')

    def updated(self, name, value):
        worker = self.sender()
        if self.closing or self.workers.get(worker.kind) is not worker:
            return
        self.records[name] = {**value, 'checked_at': datetime.now().astimezone().isoformat(timespec='seconds'),
                              'client': worker.lease.owner.client}
        self.changed.emit()

    def finished(self):
        worker = self.sender()
        if self.workers.get(worker.kind) is worker:
            del self.workers[worker.kind]
        for name in worker.names:
            if self.records.get(name, {}).get('state') in {'CHECKING', 'STOPPED'}:
                self.records[name] = result('STOPPED', '已让出连接或停止检查；原业务资料和任务保持。')
                if not self.closing:self.defer([name])
        worker.deleteLater(); self.changed.emit()
        if self.closing and not self.workers:
            self.host.close()

    def retry_pending(self):
        if self.closing or self.paused or getattr(self.host,'_data_location_preparing',False):
            return
        if time.monotonic() >= self.retry_until:
            for record in self.records.values():
                if record['state']=='LOGIN_REQUIRED' and not record.get('auto_retry_expired'):
                    record['message']='自动复核等待已结束。请完成对应浏览器登录后点击“重新检查”。'
                    record['auto_retry_expired']=True
                    self.changed.emit()
        names = [n for n in NAMES if n in self.waiting or
                 (self.records.get(n, {}).get('state') == 'LOGIN_REQUIRED' and time.monotonic() < self.retry_until)]
        if names:
            # Background retries do not extend the five-minute login window.
            deadline = self.retry_until; self.retry(names); self.retry_until = deadline

    def prepare_close(self):
        self.closing = True; self.timer.stop()
        for worker in self.workers.values():
            worker.cancel.set()
        return not self.workers

    def resume(self):
        self.closing = False
        if self.started:
            self.timer.start()

    def presentation(self, name, transport, client):
        record = self.records.get(name)
        if not record:
            return None
        state = record['state']
        if state == 'READY' and (transport != 'CONNECTED' or record.get('client') is not client):
            return None
        text = TEXT[state]
        color = '#16805d' if state == 'READY' else '#ba4242' if state in {'WRONG_ACCOUNT', 'FAILED'} else '#a26b12'
        detail = record['message']
        if record.get('code'):
            detail += '\n原因编号：' + record['code']
        if record.get('checked_at'):
            detail += '\n最近检查：' + record['checked_at']
        return text, color, detail

    def open_details(self):
        dialog = QDialog(self.host); dialog.setWindowTitle('连接检查与修复'); dialog.resize(850, 360)
        layout = QVBoxLayout(dialog)
        note = QLabel('启动时自动连接并核验登录；这里只检查连接，不采集资料、不重发导出或采购。')
        note.setWordWrap(True); layout.addWidget(note)
        grid = QGridLayout(); grid.setColumnStretch(1,1); layout.addLayout(grid); labels = {}
        for i, name in enumerate(NAMES):
            grid.addWidget(QLabel(name), i, 0)
            detail = QLabel(); detail.setWordWrap(True); grid.addWidget(detail, i, 1); labels[name] = detail
            grid.addWidget(button('重新检查', lambda checked=False, n=name: self.retry([n])), i, 2)
        def refresh():
            for name, label in labels.items():
                record = self.records.get(name, result('WAITING', '尚未检查'))
                label.setText(TEXT[record['state']] + '：' + record['message'] +
                              ('\n原因编号：' + record['code'] if record.get('code') else ''))
        timer = QTimer(dialog); timer.setInterval(500); timer.timeout.connect(refresh); timer.start(); refresh()
        row = QHBoxLayout(); layout.addLayout(row)
        def configure(kind):
            if self.workers:
                note.setText('正在安全结束自动检查，请等待状态更新后再打开配置。')
                for worker in self.workers.values(): worker.cancel.set()
                return
            dialog.accept()
            if kind == 'ax': self.host.open_aoxiang_setup()
            else:
                from catalog_sync_ui import open_sync
                open_sync(self.host, [kind])
                self.retry([kind])
        row.addWidget(button('翱象配置', lambda: configure('ax')))
        for name in NAMES[1:]: row.addWidget(button(name + '配置', lambda checked=False, n=name: configure(n)))
        row.addStretch(); row.addWidget(button('关闭', dialog.accept)); dialog.exec()
