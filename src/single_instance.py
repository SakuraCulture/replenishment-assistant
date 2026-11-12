"""One GUI per Windows user, with a local, user-only activation pipe.

Acquired before opening application data. Worker entrypoints do not call main().
The lock is outside movable data roots and Qt checks dead owners on next launch.
"""
import hashlib,os,time
from pathlib import Path
from PySide6.QtCore import QObject,QLockFile,Qt
from PySide6.QtNetwork import QLocalServer,QLocalSocket

class DetachedGuard:
    """QLockFile has no QObject parent and survives QApplication teardown."""
    def __init__(self,lock):self.lock=lock
    def close(self):
        if self.lock is not None:self.lock.unlock();self.lock=None

def scope(arguments,data_root):
    import json
    marker=Path(data_root).resolve().parent/'synthetic.json';marked=False
    if marker.is_file() and marker.stat().st_size<=65536:
        try:marked=json.loads(marker.read_text('utf-8')).get('synthetic') is True
        except (ValueError,OSError,AttributeError):pass
    if '--data-location-smoke' in arguments:
        i=arguments.index('--data-location-smoke')
        try:
            out=Path(arguments[i+1]).resolve()
            spec=json.loads((out/'synthetic.json').read_text('utf-8'))
            import re
            local=Path(spec.get('localappdata','')).resolve()
            if spec.get('synthetic') is True and spec.get('live_platform') is False and Path(os.environ.get('LOCALAPPDATA','')).resolve()==local and local.parent==Path(os.environ['TEMP']).resolve() and re.fullmatch('replenishment-location-[a-f0-9]{16}',local.name):
                return 'qa-location:'+str(out).casefold()
        except (ValueError,OSError,IndexError,AttributeError):pass
    isolated=marked and any(a.startswith('--') and (a.endswith('-smoke') or a=='--business-flow-acceptance') for a in arguments)
    return 'qa:'+str(Path(data_root).resolve()).casefold() if isolated else 'main'


class SingleInstance(QObject):
    def __init__(self,scope='main',*,folder=None,parent=None):
        super().__init__(parent)
        base=Path(folder or Path(os.environ.get('LOCALAPPDATA',Path.home()))/'YoutuanTools'/'instance')
        base.mkdir(parents=True,exist_ok=True)
        key=hashlib.sha256((str(Path.home()).casefold()+'|'+scope).encode()).hexdigest()[:32]
        self.name='Youtuan-Replenishment-'+key;self.scope=scope
        self.lock=QLockFile(str(base/(key+'.lock')));self.lock.setStaleLockTime(0)
        self.server=None;self.window=None;self.pending=False;self.owned=False;self.clients=set();self.buffers={};self.closing=False

    def acquire(self):
        if not self.lock.tryLock(0):
            self.activate_existing();return False
        self.owned=True
        if self.scope=='main':
            from application_processes import foreground_legacy
            try:
                if foreground_legacy():self.close();return False
            except Exception:self.close();raise
        self.server=QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        # Only the lock owner may remove a pipe left by a crashed process.
        QLocalServer.removeServer(self.name)
        if not self.server.listen(self.name):
            self.close();raise RuntimeError('已有窗口通信未建立，未打开第二个业务窗口')
        self.server.newConnection.connect(self.accept)
        return True

    def accept(self):
        while self.server and self.server.hasPendingConnections():
            client=self.server.nextPendingConnection();self.clients.add(client)
            client.readyRead.connect(lambda c=client:self.read(c))
            client.disconnected.connect(lambda c=client:self.drop(c))
            client.write((str(os.getpid())+'\n').encode());client.flush()
            if client.bytesAvailable():self.read(client)

    def drop(self,client):
        from shiboken6 import isValid
        self.clients.discard(client);self.buffers.pop(client,None)
        if isValid(client):client.deleteLater()

    def read(self,client):
        data=self.buffers.get(client,b'')+bytes(client.readAll());self.buffers[client]=data
        if len(data)<6 and b'\n' not in data:return
        if data==b'focus\n':
            if self.closing:client.write(b'BUSY\n')
            else:self.activate();client.write(b'OK\n')
            client.flush();client.disconnectFromServer()
        elif data:client.abort()

    def bind(self,window):
        self.window=window
        if self.pending:self.activate()

    def activate(self):
        from shiboken6 import isValid
        if self.closing or self.window is not None and not isValid(self.window):return
        if self.window is None:self.pending=True;return
        self.pending=False;window=self.window
        window.setWindowState((window.windowState() & ~Qt.WindowState.WindowMinimized)|Qt.WindowState.WindowActive)
        window.show();window.raise_();window.activateWindow()
        from PySide6.QtWidgets import QApplication
        target=QApplication.activeModalWidget() or window
        if target is not window:target.show();target.raise_();target.activateWindow()
        if os.name=='nt':
            import ctypes
            handle=int(target.winId());ctypes.windll.user32.ShowWindow(handle,9 if target.isMinimized() else 5)
            ctypes.windll.user32.SetForegroundWindow(handle)

    def activate_existing(self):
        from data_location import pending_message
        message=pending_message() if self.scope.startswith('qa-location:') or self.scope=='main' and not os.environ.get('REPLENISHMENT_DATA_DIR') else ''
        if message:raise RuntimeError(message)
        deadline=time.monotonic()+12
        while time.monotonic()<deadline:
            client=QLocalSocket(self);client.connectToServer(self.name)
            if client.waitForConnected(300):
                if not client.canReadLine():client.waitForReadyRead(5000)
                pid=bytes(client.readLine()).strip()
                if pid.isdigit():
                    if os.name=='nt':
                        import ctypes
                        ctypes.windll.user32.AllowSetForegroundWindow(int(pid))
                    client.write(b'focus\n');client.waitForBytesWritten(1000)
                    if not client.canReadLine():client.waitForReadyRead(5000)
                    reply=bytes(client.readLine()).strip()
                    if reply==b'OK':client.abort();client.deleteLater();return
                    if reply==b'BUSY':client.abort();client.deleteLater();raise RuntimeError('原实例正在安全退出或迁入资料；请等待结束，未打开第二个业务窗口')
            client.abort();client.deleteLater();time.sleep(.05)
        raise RuntimeError('补货助手已在运行，正在启动或未响应。未再打开业务窗口；请稍后再次打开以唤醒原窗口。')

    def close(self):
        self.closing=True;self.window=None
        for client in list(self.clients):client.abort()
        self.clients.clear()
        self.buffers.clear()
        if self.server:self.server.close();self.server.deleteLater();self.server=None
        if self.owned:self.lock.unlock();self.owned=False

    def quiesce(self):
        # Retain the lock through apply; late activation cannot resurrect widgets.
        self.closing=True;self.window=None
        if self.server:self.server.close()

    def take_guard(self):
        """Transfer ownership without a moment in which another writer can enter."""
        if not self.owned:raise RuntimeError('未持有实例保护，未执行资料搬迁')
        self.quiesce()
        for client in list(self.clients):client.abort()
        self.clients.clear();self.buffers.clear()
        if self.server:
            from shiboken6 import delete
            self.server.close();delete(self.server);self.server=None
        guard=DetachedGuard(self.lock);self.lock=None;self.owned=False
        return guard
