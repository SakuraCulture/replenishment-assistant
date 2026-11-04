"""Cooperative application shutdown of the retained JST component, off the GUI thread."""
from pathlib import Path
from PySide6.QtCore import QObject, QTimer
from aoxiang_ui_common import Call
from catalog_sync_session import SessionOwner


class Manager(QObject):
    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.session = SessionOwner(Path(host.repository.folder) / 'jst_catalog_sync')
        self.worker = None
        host.destroyed.connect(self.shutdown)

    def prepare_close(self):
        if self.worker is not None:
            return False
        if self.session.lease is not None:
            self.host.status.setText('等待聚水潭商品资料窗口安全结束当前操作…')
            return False
        if self.session.client is None:
            return True
        self.host.status.setText('正在结束保留的聚水潭连接；不会关闭你的浏览器…')
        task = Call(lambda _: self.session.close(timeout=.5), self)
        self.worker = task
        task.failed.connect(lambda text: self.host.status.setText('聚水潭连接尚未退出：' + text))
        def finished():
            self.worker = None
            task.deleteLater()
            QTimer.singleShot(200, self.host.close)
        task.finished.connect(finished)
        task.start()
        return False

    def shutdown(self):
        if self.worker is None and self.session.client is not None:
            self.session.close(timeout=.5)


def manager(host):
    if not hasattr(host, 'catalog_sessions'):
        host.catalog_sessions = Manager(host)
    return host.catalog_sessions
