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
