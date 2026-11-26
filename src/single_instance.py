"""One GUI per Windows user, with a local, user-only activation pipe.

Acquired before opening application data. Worker entrypoints do not call main().
The lock is outside movable data roots and Qt checks dead owners on next launch.
"""
import hashlib,logging,os,time
from contextlib import contextmanager
from pathlib import Path
from PySide6.QtCore import QObject,QLockFile,QStandardPaths,Qt
from PySide6.QtNetwork import QLocalServer,QLocalSocket

class DetachedGuard:
    """QLockFile has no QObject parent and survives QApplication teardown."""
    def __init__(self,lock):self.lock=lock
    def close(self):
        if self.lock is not None:self.lock.unlock();self.lock=None

def scope(arguments,data_root):
    import json
    root=Path(data_root).resolve()
    marker=root.parent/'synthetic.json';marked=False;offline=False
    if marker.is_file() and marker.stat().st_size<=65536:
        try:
            spec=json.loads(marker.read_text('utf-8'))
            marked=spec.get('synthetic') is True
            offline=marked and spec.get('live_platform') is False
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
    override=os.environ.get('REPLENISHMENT_DATA_DIR')
    isolated=offline and override and Path(override).resolve()==root
    isolated=isolated or marked and any(a.startswith('--') and (a.endswith('-smoke') or a=='--business-flow-acceptance') for a in arguments)
    return 'qa:'+str(Path(data_root).resolve()).casefold() if isolated else 'main'


def process_endpoint(pid):
    return f'Youtuan-Replenishment-Window-{pid}'


@contextmanager
def greeting(name, *, wait=500):
    """Read the owner's PID without focusing or changing any window."""
    client=QLocalSocket()
    try:
        client.connectToServer(name)
        pid=None
        if client.waitForConnected(200):
            if not client.canReadLine():client.waitForReadyRead(wait)
            line=bytes(client.readLine()).strip()
            if line.isdigit():pid=int(line)
        yield client,pid
    finally:
        client.abort()


class SingleInstance(QObject):
    def __init__(self,scope='main',*,folder=None,parent=None,data_root=None):
        super().__init__(parent)
        base=Path(folder or Path(os.environ.get('LOCALAPPDATA',Path.home()))/'YoutuanTools'/'instance')
        base.mkdir(parents=True,exist_ok=True)
        key=hashlib.sha256((str(Path.home()).casefold()+'|'+scope).encode()).hexdigest()[:32]
        self.name='Youtuan-Replenishment-'+key;self.scope=scope
        self.lock=QLockFile(str(base/(key+'.lock')));self.lock.setStaleLockTime(0)
        self.data_root=data_root
        # Older launchers use the user-wide endpoint. A redirected diagnostics
        # profile must not advertise itself there as the user's business window.
        canonical=Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation))/'YoutuanTools/instance'
        self.public_alias=scope!='main' or os.name!='nt' or base.resolve()==canonical.resolve()
        self.servers=[];self.alias_peer=None;self.window=None;self.pending=False;self.owned=False;self.clients=set();self.buffers={};self.closing=False

    def independent_windows(self,peers):
        from application_reset import lease_is_idle
        from application_processes import packaged_with_data_lease
        if not peers or self.data_root is None or not lease_is_idle(self.data_root):
            return set()
        # We hold this profile's primary lock. A peer speaking our protocol
        # with a live window and no lease on our data belongs to another profile.
        separate={pid for pid,image in peers.items() if packaged_with_data_lease(image)}
        others=set(peers)-separate
        if not others:return separate
        with greeting(self.name) as (_,older_pid):
            separate.update(others & {older_pid})
        for pid in others-separate:
            with greeting(process_endpoint(pid)) as (_,owner):
                if owner==pid:separate.add(pid)
        return separate

    def acquire(self):
        if not self.lock.tryLock(0):
            self.activate_existing();return False
        self.owned=True
        if self.scope=='main':
            from application_processes import foreground_legacy
            try:
                if foreground_legacy(self.independent_windows):self.close();return False
            except Exception:self.close();raise
        self.listen(process_endpoint(os.getpid()))
        if self.public_alias:
            self.publish_alias()
        return True

    def publish_alias(self):
        if self.alias_peer:
            self.alias_peer.deleteLater()
            self.alias_peer=None
        if self.closing:return
        peer=QLocalSocket(self)
        peer.connectToServer(self.name)
        if peer.waitForConnected(200):
            # An older isolated build already owns this address. Keep a passive
            # connection so older business launchers can find us once it exits.
            self.alias_peer=peer
            peer.disconnected.connect(self.publish_alias,Qt.ConnectionType.QueuedConnection)
            return
        peer.deleteLater()
        self.listen(self.name,required=False)

    def listen(self,name,*,required=True):
        server=QLocalServer(self)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        QLocalServer.removeServer(name)
        if not server.listen(name):
            reason=server.errorString()
            server.deleteLater()
            if required:
                self.close()
                raise RuntimeError('已有窗口通信未建立，未打开第二个业务窗口：'+reason)
            logging.warning('旧版工作台唤醒入口未建立：%s',reason)
            return
        self.servers.append(server)
        server.newConnection.connect(lambda:self.accept(server))

    def accept(self,server):
        while server.hasPendingConnections():
            client=server.nextPendingConnection();self.clients.add(client)
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
            lock_info=self.lock.getLockInfo()
            expected=lock_info[0] if lock_info else 0
            if expected>0:
                for name in (process_endpoint(expected),self.name):
                    with greeting(name,wait=1500) as (client,pid):
                        if pid!=expected:continue
                        reply=self.focus(client,pid)
                        if reply==b'OK':return
                        if reply==b'BUSY':raise RuntimeError('原实例正在安全退出或迁入资料；请等待结束，未打开第二个业务窗口')
            time.sleep(.05)
        raise RuntimeError('补货助手已在运行，正在启动或未响应。未再打开业务窗口；请稍后再次打开以唤醒原窗口。')

    @staticmethod
    def focus(client,pid):
        if os.name=='nt':
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(pid)
        client.write(b'focus\n');client.waitForBytesWritten(1000)
        if not client.canReadLine():client.waitForReadyRead(5000)
        return bytes(client.readLine()).strip()

    def close(self):
        self.quiesce()
        for client in list(self.clients):client.abort()
        self.clients.clear()
        self.buffers.clear()
        for server in self.servers:server.deleteLater()
        self.servers.clear()
        if self.owned:self.lock.unlock();self.owned=False

    def quiesce(self):
        # Retain the lock through apply; late activation cannot resurrect widgets.
        self.closing=True;self.window=None
        if self.alias_peer:
            self.alias_peer.blockSignals(True);self.alias_peer.abort();self.alias_peer.deleteLater();self.alias_peer=None
        for server in self.servers:server.close()

    def take_guard(self):
        """Transfer ownership without a moment in which another writer can enter."""
        if not self.owned:raise RuntimeError('未持有实例保护，未执行资料搬迁')
        self.quiesce()
        for client in list(self.clients):client.abort()
        self.clients.clear();self.buffers.clear()
        from shiboken6 import delete
        for server in self.servers:delete(server)
        self.servers.clear()
        guard=DetachedGuard(self.lock);self.lock=None;self.owned=False
        return guard
