"""Dedicated JST process transport. Independent of AX session, queue and cancellation."""
from __future__ import annotations
import json
import os
import queue
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from priority_list import atomic_json

PROTOCOL='jst.catalog.v1'


class ComponentError(RuntimeError):
    def __init__(self, code, message, details=None):
        super().__init__(message); self.code=code;self.details=details or {}


def components():
    base=Path(__file__).resolve().parent
    folder=base/'components' if getattr(sys,'frozen',False) else base.parent/'components'
    return folder/'jst_catalog',folder/'aoxiang'


class Client:
    def __init__(self, folder, *, forbidden_roots=(), component_roots=None):
        self.folder=Path(folder).resolve(); self.roots=component_roots or components()
        self.forbidden_roots=list(map(str,forbidden_roots)); self.process=None; self.pending={}
        self.lock=threading.Lock(); self.write_lock=threading.Lock(); self.ready=threading.Event(); self.error=None
        self.started=False; self.stderr=bytearray();self.options_file=None
    @property
    def running(self):return self.process is not None and self.process.poll() is None
    def start(self):
        if self.started:raise ComponentError('ALREADY_STARTED','该采集连接已启动，请查询原服务状态')
        self.started=True; jst,ax=map(Path,self.roots)
        node=ax/'runtime/node.exe'; entry=jst/'src/machine-entry.mjs'
        if not node.is_file() or not entry.is_file():raise ComponentError('COMPONENT_MISSING','聚水潭采集组件缺失，请使用完整交付文件夹')
        if getattr(sys,'frozen',False):
            from catalog_sync_bundle import verify_component
            verify_component(jst,ax)
        validator={'executable':sys.executable,'prefix':['--catalog-validate'] if getattr(sys,'frozen',False) else [str(Path(__file__).parent/'app.py'),'--catalog-validate']}
        ax_binding=self.folder.parent/'aoxiang/config/machine.binding.json'
        forbidden=list(self.forbidden_roots)
        if ax_binding.exists():
            binding=json.loads(ax_binding.read_text('utf-8'))
            if binding.get('userDataRoot'):forbidden.append(binding['userDataRoot'])
        options={'stateRoot':str(self.folder),'bridgeRoot':str(ax),'lockRoot':str(self.folder.parent/'jst_catalog_browser_locks'),
                 'forbiddenRoots':forbidden,'validator':validator,'parentPid':os.getpid()}
        from data_location import aliases_for
        options['storageAliases']=aliases_for(self.folder)
        opts=self.folder/('runtime-options-'+uuid.uuid4().hex+'.json');atomic_json(opts,options);self.options_file=opts
        self.process=subprocess.Popen([str(node),str(entry),'--options',str(opts)],cwd=jst,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.reader=threading.Thread(target=self._read,daemon=True,name='jst-catalog-protocol');self.reader.start()
        self.diagnostics=threading.Thread(target=self._stderr,daemon=True,name='jst-catalog-diagnostics');self.diagnostics.start()
        if not self.ready.wait(15):raise ComponentError('STARTUP_UNKNOWN','采集服务启动结果未知，请先检查原进程')
        if self.error:raise self.error
        return self.request('capabilities')
    def _stderr(self):
        while block:=self.process.stderr.read(4096):
            self.stderr.extend(block);del self.stderr[:-65536]
    def _fail(self,error):
        self.error=error;self.ready.set()
        with self.lock:pending=list(self.pending.values())
        for q in pending:
            try:q.put_nowait(error)
            except queue.Full:pass
    def _read(self):
        try:
            while line:=self.process.stdout.readline(1024*1024+1):
                if len(line)>1024*1024 or not line.endswith(b'\n'):raise ValueError('响应长度异常')
                frame=json.loads(line)
                if frame.get('protocol')!=PROTOCOL:raise ValueError('协议版本不匹配')
                if frame['type']=='ready':self.ready.set()
                elif frame['type']=='fatal':raise ComponentError(frame['error']['code'],frame['error']['message'],frame['error'].get('details'))
                elif frame['type']=='response':
                    with self.lock:q=self.pending.get(frame.get('request_id'))
                    if q:q.put_nowait(frame)
                else:raise ValueError('未知响应类型')
        except Exception as exc:
            self._fail(exc if isinstance(exc,ComponentError) else ComponentError('PROTOCOL_FAILED','采集服务响应异常：'+str(exc)))
        finally:self._fail(self.error or ComponentError('COMPONENT_EXITED','采集服务已退出；已受理任务请按原记录恢复'))
    def request(self,action,*,timeout=15,**values):
        if self.error:raise self.error
        if not self.running or not self.ready.is_set():raise ComponentError('NOT_READY','采集服务未就绪')
        if {'request_id','action','protocol'}&values.keys():raise ValueError('不能覆盖协议关联字段')
        ident=uuid.uuid4().hex;q=queue.Queue(maxsize=1)
        body=(json.dumps({'protocol':PROTOCOL,'request_id':ident,'action':action,**values},ensure_ascii=False,allow_nan=False)+'\n').encode('utf-8')
        if len(body)>1024*1024:raise ValueError('请求过大')
        with self.lock:self.pending[ident]=q
        try:
            with self.write_lock:self.process.stdin.write(body);self.process.stdin.flush()
            try:frame=q.get(timeout=timeout)
            except queue.Empty:raise ComponentError('RESPONSE_UNKNOWN','响应超时；请查询原任务，不能据此重复提交')
            if isinstance(frame,Exception):raise frame
            if frame.get('ok') is not True:
                e=frame.get('error',{});raise ComponentError(e.get('code','FAILED'),e.get('message','采集操作失败'),e.get('details'))
            return frame.get('result')
        finally:
            with self.lock:self.pending.pop(ident,None)
    def close(self,timeout=3):
        if self.process is None:
            if self.options_file:self.options_file.unlink(missing_ok=True)
            return True
        if self.running and not self.process.stdin.closed and not self.error:
            try:self.request('shutdown',timeout=min(timeout,2))
            except Exception:pass
        # Also close the writer when the child exited first (parent-watch,
        # startup error or protocol failure), otherwise the host leaks handles.
        with self.write_lock:
            if not self.process.stdin.closed:
                try:self.process.stdin.close()
                except OSError:pass
        if self.running:
            try:self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:return False
        self.reader.join(timeout=1);self.diagnostics.join(timeout=1)
        for pipe in (self.process.stdout,self.process.stderr):pipe.close()
        if self.options_file:self.options_file.unlink(missing_ok=True)
        return True
