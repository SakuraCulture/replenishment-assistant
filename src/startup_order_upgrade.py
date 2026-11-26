"""Run the pre-upgrade SQLite backup while the first visible window stays responsive."""
from contextlib import closing

from PySide6.QtCore import QEventLoop, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import QProgressDialog

import normal_orders
import work_repository


class OrderUpgrade(QThread):
    progress = Signal(str, int)

    def __init__(self, path):
        super().__init__()
        self.work = work_repository.Repository.open_existing(path)
        self.failure = None
        self.last_progress = None

    def publish(self, stage):
        total = getattr(stage, 'total', 0)
        percent = (100 * stage.current // total) if total else -1
        update = (str(stage), percent)
        if update != self.last_progress:
            self.last_progress = update
            self.progress.emit(*update)

    def run(self):
        try:
            normal_orders.Repository(self.work, backup_progress=self.publish)
        except Exception as exc:
            self.failure = exc


class UpgradeNotice(QProgressDialog):
    @Slot(str, int)
    def update_progress(self, text, percent):
        self.setLabelText(text)
        self.setRange(0, 100 if percent >= 0 else 0)
        if percent >= 0:
            self.setValue(percent)


def prepare_existing_orders(path):
    if not path.is_file():
        return
    with closing(work_repository.read_connection(path)) as reader:
        if not normal_orders.needs_order_header_backup(reader):
            return

    # No workbench or business timers have been constructed. The local event
    # loop can paint and service activation requests for the primary instance.
    dialog = UpgradeNotice('正在准备升级前的订单资料备份', None, 0, 0)
    dialog.setWindowTitle('补货助手 · 正在升级订单资料')
    dialog.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.CustomizeWindowHint | Qt.WindowType.WindowTitleHint)
    dialog.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
    dialog.setAutoClose(False)
    dialog.setAutoReset(False)
    dialog.setMinimumDuration(0)
    worker = OrderUpgrade(path)
    worker.progress.connect(dialog.update_progress)
    loop = QEventLoop()
    worker.finished.connect(loop.quit)
    dialog.show()
    QTimer.singleShot(0, worker.start)
    try:
        loop.exec()
    finally:
        worker.wait()
        dialog.hide()
    if worker.failure is not None:
        raise worker.failure
