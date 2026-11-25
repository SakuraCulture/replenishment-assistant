"""Shared JST transport with exclusive maintenance and scoped collection leases."""
import threading
import time
import uuid
from pathlib import Path
from catalog_sync_client import Client, ComponentError
from runtime_support import checkpoint


class SessionOwner:
    def __init__(self, folder, factory=Client, resources=None):
        self.folder, self.factory = Path(folder).resolve(), factory
        self.client, self.capabilities = None, {}
        self.ready, self.resources = False, resources
        self.lock = threading.RLock()
        self.leases = {}

    @property
    def lease(self):
        with self.lock:
            return next(iter(self.leases.values()), None)

    def acquire(self, warehouses=None):
        scope = frozenset(warehouses or ())
        if scope - {'山东仓', '河北仓'}:
            raise ValueError('聚水潭操作仓无效')
        with self.lock:
            limit = self.resources.warehouse_slots() if scope and self.resources else 1
            if limit < 2:
                scope = frozenset()
            if self.leases and (not scope or not self.capabilities.get('warehouse_slots') or any(
                    not item.scope or item.scope & scope for item in self.leases.values())):
                raise ComponentError('BUSY', '所需聚水潭浏览器在使用中，等待原操作安全结束')
            lease = SessionLease(self, scope)
            self.leases[lease.id] = lease
            return lease

    def acquire_wait(self, warehouses, cancel, progress):
        deadline = time.monotonic() + 900
        while True:
            checkpoint(cancel)
            try:
                return self.acquire(warehouses)
            except ComponentError as error:
                if error.code != 'BUSY' or time.monotonic() >= deadline:
                    raise
            progress('等待聚水潭浏览器可用 · ' + '、'.join(warehouses))
            cancel.wait(.5)

    def start(self):
        with self.lock:
            if self.client is not None:
                if self.ready and self.client.running and not self.client.error:
                    return
                if self.client.running and not self.client.error:
                    raise ComponentError('CONNECTION_NEEDS_REVIEW', '原采集连接未就绪；请先安全结束原连接，不重复启动')
                if self.client.running and len(self.leases) > 1:
                    raise ComponentError('CONNECTION_NEEDS_REVIEW', '原聚水潭连接尚有活动仓项，等待原操作释放')
                if not self.close():
                    raise ComponentError('CLOSING', '原采集进程尚未安全退出')
            self.client = self.factory(self.folder)
            self.ready = False
            self.capabilities = self.client.start() or {}
            self.ready = True

    def read_status(self, **values):
        with self.lock:
            self.start()
            client = self.client
        return client.request('sync.status', **values)

    def close(self, timeout=.5):
        with self.lock:
            if self.client is not None and not self.client.close(timeout=timeout):
                return False
            self.client, self.ready, self.capabilities = None, False, {}
            return True


class SessionLease:
    def __init__(self, owner, scope):
        self.owner, self.scope, self.id = owner, scope, uuid.uuid4().hex
        self.started = self.released = self.closing = False

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
        if self.scope and self.owner.capabilities.get('warehouse_slots'):
            values = {**values, 'scoped': True, 'lease_id': self.id, 'scope': sorted(self.scope)}
        return self.owner.client.request(action, **values)

    def reconnect(self, warehouse):
        """Explicit recovery; healthy components disconnect only the selected account."""
        if self.released:
            raise ComponentError('SESSION_RELEASED', '当前窗口已释放聚水潭操作权')
        client = self.owner.client
        if client is not None and (not client.running or client.error):
            if client.running and len(self.owner.leases) > 1 or not self.owner.close():
                raise ComponentError('CLOSING', '原采集连接正在安全退出，请等待后再重连')
            self.started = False
        self.start()
        return self.request('session.reset', warehouse=warehouse)

    def close(self, timeout=.5):
        if self.released:
            return True
        with self.owner.lock:
            if self.owner.leases.get(self.id) is not self:
                raise ComponentError('SESSION_CONFLICT', '聚水潭操作权已变化')
            self.closing = True
            client = self.owner.client
            if client is not None and self.started:
                acknowledged = False
                if self.owner.ready and client.running and not client.error:
                    try:
                        if not self.request('session.release', timeout=max(5, timeout))['idle']:
                            return False
                        acknowledged = True
                    except Exception:
                        pass
                if not acknowledged and (client.running and any(not item.closing for item in self.owner.leases.values())
                                         or not self.owner.close(timeout)):
                    # Another warehouse may still be using the original process.
                    return False
            elif client is not None and not self.owner.ready and not self.owner.close(timeout):
                return False
            del self.owner.leases[self.id]
            self.released = True
        return True
