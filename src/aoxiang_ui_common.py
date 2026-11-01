"""Shared component calls and cooperative close for AX business dialogs."""
from pathlib import Path
import sys
from PySide6.QtCore import QThread, Signal, QTimer
from PySide6.QtWidgets import QDialog, QMessageBox
from aoxiang_client import ProcessClient


def component_root():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent)) / 'components/aoxiang'


class Call(QThread):
    succeeded = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, operation, parent):
        super().__init__(parent)
        self.operation = operation

    def run(self):
        try:
            self.succeeded.emit(self.operation(self.progress.emit))
        except Exception as exc:
            code = getattr(exc, 'code', '')
            self.failed.emit((code + '：' if code else '') + str(exc))


class ComponentDialog(QDialog):
    def run(self, operation, success):
        if self.worker:
            return
        worker = Call(operation, self)
        self.worker = worker
        self.update_buttons()
        worker.progress.connect(self.status.setText)
        worker.succeeded.connect(success)
        worker.failed.connect(self.failure)
        worker.finished.connect(self.task_finished)
        worker.start()

    def task_finished(self):
        previous, self.worker = self.worker, None
        previous.deleteLater()
        self.update_buttons()
        if self._closing:
            if self._close_done:
                self._can_close = True
                self._closing = False
                self.close()
            else:
                QTimer.singleShot(600, self.finish_close)
        else:
            self.on_idle()

    def on_idle(self):
        pass

    def failure(self, text):
        self.status.setText('未完成：' + text)
        if not self._closing:
            QMessageBox.warning(self, '翱象操作未完成', text)

    def ensure_client(self):
        if self.started and not self.client.running:
            self.client.close()
            self.client = self.client_factory(component_root(), self.folder / 'component-state')
            self.started = False
        if not self.started and getattr(self.client, '_process', None) is not None:
            if self.client.running:
                raise RuntimeError('现有组件仍在启动或等待退出，请先关闭本窗口再重新连接')
            self.client.close()
            self.client = self.client_factory(component_root(), self.folder / 'component-state')
        if not self.started:
            self.client.start()
            self.started = True

    def reject(self):
        self.close()

    def closeEvent(self, event):
        if self._can_close:
            event.accept()
            return
        event.ignore()
        self._closing = True
        self.poll.stop()
        self.status.setText('正在安全关闭连接，等待当前网页动作结束；不会关闭你的 Edge。')
        self.update_buttons()
        self.finish_close()

    def finish_close(self):
        if not self.worker:
            self.run(lambda _: self.client.close(timeout=.5), self.closed_result)

    def closed_result(self, done):
        self._close_done = bool(done)


def show_modal(host, dialog):
    host._aoxiang_dialog = dialog
    try:
        dialog.exec()
    finally:
        host._aoxiang_dialog = None
        dialog.deleteLater()
        requested = getattr(host, '_aoxiang_close_requested', False)
        if requested:
            host._aoxiang_close_requested = False
            QTimer.singleShot(0, host.close)
    return not requested
