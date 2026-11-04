"""Application-owned JST transport; windows exclusively borrow operation access."""
import threading
from pathlib import Path
from catalog_sync_client import Client, ComponentError


class SessionOwner:
    def __init__(self, folder, factory=Client):
        self.folder, self.factory = Path(folder).resolve(), factory
        self.client = self.lease = None
        self.ready = False
        self.lock = threading.RLock()

    def acquire(self):
        with self.lock:
            if self.lease is not None:
                raise ComponentError('BUSY', '请先关闭当前聚水潭商品资料窗口，等待原任务安全结束')
            self.lease = SessionLease(self)
            return self.lease

    def start(self):
        with self.lock:
            if self.client is not None:
                if self.ready and self.client.running and not self.client.error:
                    return
                if self.client.running:
                    raise ComponentError('CONNECTION_NEEDS_REVIEW', '原采集连接未就绪；请先安全结束原连接，不重复启动')
                if not self.close():
                    raise ComponentError('CLOSING', '原采集进程尚未安全退出')
            self.client = self.factory(self.folder)
            self.ready = False
            self.client.start()
            self.ready = True

    def close(self, timeout=.5):
        with self.lock:
            if self.client is not None and not self.client.close(timeout=timeout):
                return False
            self.client, self.ready = None, False
            return True


class SessionLease:
    def __init__(self, owner):
        self.owner = owner
        self.started = self.released = False

    @property
    def folder(self):
        return self.owner.folder

    @property
    def running(self):
        return not self.released and bool(self.owner.client and self.owner.client.running)

    def start(self):
        if self.released:
            raise ComponentError('SESSION_RELEASED', '当前窗口已释放聚水潭操作权')
        self.owner.start()
        self.started = True

    def request(self, action, **values):
        if self.released or not self.started:
            raise ComponentError('SESSION_RELEASED', '尚未取得聚水潭操作权')
        return self.owner.client.request(action, **values)

    def reconnect(self, warehouse):
        """Explicit recovery; healthy components disconnect only the selected account."""
        if self.released:
            raise ComponentError('SESSION_RELEASED', '当前窗口已释放聚水潭操作权')
        client = self.owner.client
        if client is not None and (not client.running or client.error):
            if not self.owner.close():
                raise ComponentError('CLOSING', '原采集连接正在安全退出，请等待后再重连')
            self.started = False
        self.start()
        return self.request('session.reset', warehouse=warehouse)

    def close(self, timeout=.5):
        if self.released:
            return True
        client = self.owner.client
        if client is not None:
            if self.started and self.owner.ready and client.running and not client.error:
                try:
                    if not client.request('session.release', timeout=max(5, timeout))['idle']:
                        return False
                except Exception:
                    # Unknown handoff retains exclusive access until the old process exits.
                    if not self.owner.close(timeout):
                        return False
            elif not self.owner.close(timeout):
                return False
        with self.owner.lock:
            if self.owner.lease is not self:
                raise ComponentError('SESSION_CONFLICT', '聚水潭操作权已变化')
            self.owner.lease = None
            self.released = True
        return True
