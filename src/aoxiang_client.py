"""Local NDJSON transport. No Qt, business writes, browser selectors or automatic retries."""
from __future__ import annotations

import json
import os
import sqlite3
import queue
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any

MAX_FRAME_BYTES = 1024 * 1024


class ComponentError(RuntimeError):
    def __init__(self, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.code, self.details = code, details or {}


class ProcessClient:
    """One process owns a session; timeout means unknown, never permission to replay a platform action."""

    def __init__(self, component_root: Path, state_root: Path):
        self.component_root, self.state_root = Path(component_root).resolve(), Path(state_root).resolve()
        self.business_path=self.state_root.parent.parent/'workbench.sqlite3'
        self._retire_lock=threading.RLock()
        self._pending: dict[str, queue.Queue] = {}
        self._lock, self._write_lock = threading.Lock(), threading.Lock()
        self._ready = threading.Event()
        self._hello = None
        self._failure: ComponentError | None = None
        self._process: subprocess.Popen | None = None
        self._stderr = bytearray()

    def start(self, timeout: float = 30) -> dict:
        if self._process is not None:
            raise ComponentError('CLIENT_ALREADY_STARTED', '该组件连接已经启动，请使用现有连接')
        node, entry = self.component_root / 'runtime/node.exe', self.component_root / 'src/machine-entry.mjs'
        if not node.is_file() or not entry.is_file():
            raise ComponentError('COMPONENT_MISSING', '翱象组件或固定运行时缺失，请使用完整交付文件夹')
        self._process = subprocess.Popen(
            [str(node), str(entry), '--state-root', str(self.state_root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ,**({'REPLENISHMENT_BUSINESS_DB':str(self.business_path)} if self.business_path.is_file() else {})},
            cwd=self.component_root, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        self._reader = threading.Thread(target=self._read_stdout, name='aoxiang-protocol', daemon=True)
        self._diagnostics = threading.Thread(target=self._read_stderr, name='aoxiang-diagnostics', daemon=True)
        self._reader.start()
        self._diagnostics.start()
        if not self._ready.wait(timeout):
            raise ComponentError('STARTUP_UNKNOWN', '组件仍未返回启动结果；请查询现有进程，不重复启动')
        if self._failure:
            raise self._failure
        return self._hello

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _fail(self, error: ComponentError) -> None:
        with self._lock:
            if self._failure is None:
                self._failure = error
            waiting = list(self._pending.values())
        for response in waiting:
            try:
                response.put_nowait(error)
            except queue.Full:
                pass
        self._ready.set()

    def _read_stdout(self) -> None:
        try:
            while True:
                line = self._process.stdout.readline(MAX_FRAME_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_FRAME_BYTES or not line.endswith(b'\n'):
                    raise ComponentError('PROTOCOL_FRAME', '组件响应过长或不完整，结果尚未采用')
                try:
                    frame = json.loads(line.decode('utf-8'))
                except (UnicodeError, ValueError) as exc:
                    raise ComponentError('PROTOCOL_JSON', '组件响应格式无效，结果尚未采用') from exc
                if not isinstance(frame, dict) or frame.get('protocol_version') != 1:
                    raise ComponentError('PROTOCOL_VERSION', '组件接口版本不兼容')
                kind = frame.get('type')
                if kind == 'ready':
                    if self._hello is not None:
                        raise ComponentError('PROTOCOL_READY', '组件重复发送启动回执')
                    self._hello = frame
                    self._ready.set()
                elif kind == 'fatal':
                    error = frame.get('error') or {}
                    raise ComponentError(error.get('code', 'COMPONENT_FAILED'), error.get('message', '组件执行失败'), error.get('details'))
                elif kind == 'response':
                    if type(frame.get('ok')) is not bool or not isinstance(frame.get('request_id'), str):
                        raise ComponentError('PROTOCOL_RESPONSE', '组件响应缺少请求关联')
                    with self._lock:
                        pending = self._pending.get(frame['request_id'])
                    if pending is not None:
                        try:
                            pending.put_nowait(frame)
                        except queue.Full:
                            raise ComponentError('PROTOCOL_DUPLICATE', '组件对同一请求重复返回响应')
                    # A late response to a timed-out request cannot update a newer request/job.
                else:
                    raise ComponentError('PROTOCOL_RESPONSE', '组件返回未知消息类型')
        except ComponentError as exc:
            self._fail(exc)
        except Exception:
            self._fail(ComponentError('COMPONENT_DISCONNECTED', '组件连接中断，请核对原作业状态'))
        finally:
            self._fail(ComponentError('COMPONENT_EXITED', '组件已退出，已受理任务需按原命令查询'))

    def _read_stderr(self) -> None:
        while chunk := self._process.stderr.read(4096):
            with self._lock:
                self._stderr.extend(chunk)
                del self._stderr[:-65536]

    def flush_retirements(self):
        if not self.business_path.is_file():return
        import deletion_barrier
        with self._retire_lock:
            for plan in deletion_barrier.pending(self.business_path):
                complete=True
                try:
                    import export_receipts
                    export_receipts.prune_deleted(self.business_path)
                except (OSError,ValueError,sqlite3.Error,KeyError,TypeError):
                    import logging
                    logging.warning('业务已删除，导出回执清理待重试；原回执保留',exc_info=True)
                    complete=False
                for start in range(0,len(plan['oids']),256):
                    subset=plan['oids'][start:start+256]
                    values={'scopes':[s for s in plan['scopes'] if s['oid'] in subset]} if plan.get('scopes') else {}
                    r=self.request('orders.retire',operation_id=plan['operation_id'],oids=subset,**values)
                    complete=complete and not r.get('pending_files') and not r.get('pending_actions')
                if complete:
                    try:deletion_barrier.acknowledge(self.business_path,plan['operation_id'],plan['generation'])
                    except sqlite3.OperationalError:pass  # main writer busy; idempotent local cleanup resumes later

    def request(self, action: str, *, timeout: float = 30, **values) -> Any:
        if action not in ('orders.retire','shutdown') and self.running and self._hello is not None:self.flush_retirements()
        if self._failure:
            raise self._failure
        if not self.running or self._hello is None:
            raise ComponentError('COMPONENT_NOT_READY', '组件尚未就绪')
        if set(values) & {'protocol_version', 'request_id', 'action'}:
            raise ValueError('不能覆盖协议关联字段')
        request_id = str(uuid.uuid4())
        body = {'protocol_version': 1, 'request_id': request_id, 'action': action, **values}
        data = (json.dumps(body, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
        if len(data) > MAX_FRAME_BYTES:
            raise ComponentError('REQUEST_TOO_LARGE', '组件请求超过大小限制')
        response: queue.Queue = queue.Queue(maxsize=1)
        with self._lock:
            if len(self._pending) >= 32:
                raise ComponentError('CLIENT_BUSY', '组件请求过多，请等待当前查询完成')
            if self._failure:
                raise self._failure
            self._pending[request_id] = response
        try:
            with self._write_lock:
                self._process.stdin.write(data)
                self._process.stdin.flush()
            try:
                answer = response.get(timeout=timeout)
            except queue.Empty as exc:
                raise ComponentError('RESPONSE_UNKNOWN', '组件响应超时；保留原命令，需查询原状态，不能据此重复上传') from exc
            if isinstance(answer, ComponentError):
                raise answer
            if not answer['ok']:
                error = answer.get('error') or {}
                raise ComponentError(error.get('code', 'COMPONENT_FAILED'), error.get('message', '组件操作失败'), error.get('details'))
            return answer.get('result')
        except (BrokenPipeError, OSError) as exc:
            raise ComponentError('DISPATCH_UNKNOWN', '发送结果未知；需重新连接后查询原命令') from exc
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def close(self, timeout: float = 10) -> bool:
        """Request cooperative shutdown. False means still waiting; never force-kill a submission."""
        if self._process is None:
            return True
        if self.running:
            if not self._process.stdin.closed and not self._failure and self._hello is not None:
                try:
                    self.request('shutdown', timeout=min(timeout, 5))
                except ComponentError:
                    pass
            with self._write_lock:
                try:
                    self._process.stdin.close()
                except OSError:
                    pass
            try:
                self._process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                return False
        self._reader.join(timeout=1)
        self._diagnostics.join(timeout=1)
        for pipe in [self._process.stdout, self._process.stderr]:
            pipe.close()
        return True
