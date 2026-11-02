"""In-process browser-session handoff. The component's process lock remains authoritative."""
import threading


class ForegroundTicket:
    def __init__(self, gate):
        self.gate = gate

    def wait(self, cancelled=lambda: False):
        with self.gate.condition:
            while self.gate.background is not None:
                if cancelled():
                    raise RuntimeError('已取消等待后台翱象任务安全交接')
                self.gate.condition.wait(.2)
            if cancelled():
                raise RuntimeError('窗口已关闭，未启动新的翱象连接')

    def release(self):
        with self.gate.condition:
            if self.gate.foreground is self:
                self.gate.foreground = None
                self.gate.condition.notify_all()


class SessionGate:
    def __init__(self):
        self.condition = threading.Condition()
        self.background = self.foreground = None

    def request_foreground(self):
        with self.condition:
            if self.foreground is not None:
                raise RuntimeError('请先关闭当前翱象操作窗口')
            ticket = ForegroundTicket(self)
            self.foreground = ticket
            if self.background is not None:
                self.background.set()
            return ticket

    def claim_background(self):
        with self.condition:
            if self.foreground is not None or self.background is not None:
                return None
            self.background = threading.Event()
            return self.background

    def release_background(self, token):
        with self.condition:
            if self.background is not token:
                raise RuntimeError('后台会话交接标识不一致')
            self.background = None
            self.condition.notify_all()

    @property
    def yielding(self):
        with self.condition:
            return self.foreground is not None


class SessionOwner:
    """One application owns the transport; leases only own the current business operation."""
    def __init__(self, factory):
        self.factory = factory
        self.client = None
        self.started = False
        self.lock = threading.RLock()

    def start(self):
        with self.lock:
            if self.client is not None and self.started and self.client.running:
                return
            if self.client is not None:
                # An unacknowledged startup may still be running. Never spawn beside it.
                if self.client.running:
                    raise RuntimeError('共享组件尚未就绪，请使用重新连接，等待原组件安全退出')
                if not self.client.close(timeout=.5):
                    raise RuntimeError('原翱象组件尚未安全退出')
            self.started = False
            self.client = self.factory()
            self.client.start()
            self.started = True

    def close(self, timeout=.5):
        with self.lock:
            if self.client is None:
                return True
            if not self.client.close(timeout=timeout):
                return False
            self.client = None
            self.started = False
            return True

    def lease(self, ticket=None, cancelled=lambda: False):
        return SessionLease(self, ticket, cancelled)


class SessionLease:
    def __init__(self, owner, ticket, cancelled):
        self.owner, self.ticket, self.cancelled = owner, ticket, cancelled
        self.started = self.released = False

    @property
    def running(self):
        return not self.released and self.owner.started and bool(self.owner.client and self.owner.client.running)

    def start(self):
        if self.released:
            raise RuntimeError('当前窗口已经释放翱象操作权')
        if self.ticket:
            self.ticket.wait(self.cancelled)
        if self.cancelled():
            raise RuntimeError('窗口已关闭，未开始新的翱象动作')
        self.owner.start()
        self.started = True

    def request(self, action, **values):
        if not self.started or self.released:
            raise RuntimeError('尚未取得翱象操作权，不能执行请求')
        return self.owner.client.request(action, **values)

    def reset(self, timeout=.5):
        """Explicit reconnect only; keep the lease while the old host drains."""
        done = self.owner.close(timeout)
        if done:
            self.started = False
        return done

    def close(self, timeout=.5):
        if self.released:
            return True
        if self.started and self.owner.client is not None:
            client = self.owner.client
            if client.running and not getattr(client, '_failure', None):
                try:
                    if not client.request('session.release', timeout=max(timeout, 5))['idle']:
                        return False
                except Exception:
                    # Unknown release: close cooperatively; never grant a new owner early.
                    if not self.owner.close(timeout):
                        return False
            elif not self.owner.close(timeout):
                return False
        self.released = True
        if self.ticket:
            self.ticket.release()
        return True
