"""Temporary Qt-to-worker bridge. Only constructed after explicit use/due schedule."""
from PySide6.QtCore import QThread,Signal
import multiprocessing
import time
from pathlib import Path


class Bridge:
    AX_ACTIONS={'configuration','reserve','status','resume','retry','pause'}
    JST_ACTIONS={'sync.start','sync.status','sync.resume','sync.stop','sync.provide_observation','sync.acknowledge_adoption'}

    def __init__(self,host,cancel,progress):
        self.host,self.cancel,self.progress=host,cancel,progress
        self.ax=self.jst=self.token=None;self.last_ax=None;self.jst_operation=False

    def yielding(self):
        return bool(self.token and self.token.is_set())

    def request(self,payload):
        service=payload['service']
        if service=='ax':
            action=payload['action']
            if action not in self.AX_ACTIONS:raise ValueError('分析不允许该翱象操作')
            if self.ax is None:
                from browser_startup import yield_startup
                yield_startup(self.host,'ax',progress=self.progress,cancel=self.cancel)
                manager=self.host.aoxiang_workflows
                self.token=manager.gate.claim_background()
                if self.token is None:raise ValueError('翱象被原业务占用，请稍后继续分析原任务')
                self.ax=manager.session.lease(cancelled=lambda:self.cancel.is_set() or self.yielding())
                self.ax.start()
            if self.yielding():self.cancel.set()
            self.last_ax=payload.get('values',{}).get('command_id') or payload.get('values',{}).get('command',{}).get('command_id') or self.last_ax
            if self.cancel.is_set() and action in {'reserve','resume','retry'}:
                raise ValueError('已请求安全暂停，未启动后续导出；原任务保留')
            return self.ax.request(action,**payload.get('values',{}))
        if service=='jst':
            action=payload['action']
            if action not in self.JST_ACTIONS:raise ValueError('分析不允许该聚水潭操作')
            if self.jst is None:
                from browser_startup import yield_startup
                yield_startup(self.host,'jst',progress=self.progress,cancel=self.cancel)
                self.jst=self.host.catalog_sessions.session.acquire();self.jst.start()
            if action in {'sync.start','sync.resume'}:self.jst_operation=True
            if self.cancel.is_set() and action in {'sync.start','sync.resume','sync.provide_observation'}:
                raise ValueError('已请求安全暂停，未启动后续仓导出；原任务保留')
            return self.jst.request(action,**payload.get('values',{}))
        if service=='ax_root':return str(self.host.aoxiang_workflows.store.folder/'component-state')
        if service=='jst_root':return str(self.host.catalog_sessions.session.folder)
        if service=='release_ax':self.release_ax();return True
        if service=='release_jst':self.release_jst();return True
        if service=='observation':
            path=Path(self.host.work_repository.path)
            if not path.is_file():raise ValueError('原业务库缺失，不能猜测仓查询依据')
            from catalog_sync import stock_observation
            return stock_observation(path,payload['warehouse'])
        raise ValueError('未知分析会话请求')

    def release_ax(self):
        if self.ax is not None:
            while not self.ax.close(timeout=.5):
                self.progress('等待翱象原动作安全结束，不会强杀任务');time.sleep(.3)
            self.ax=None
        if self.token is not None:
            self.host.aoxiang_workflows.gate.release_background(self.token);self.token=None

    def release_jst(self):
        if self.jst is not None:
            while not self.jst.close(timeout=.5):
                self.progress('等待仓资料原动作安全结束，不会强杀任务');time.sleep(.3)
            self.jst=None

    def close(self):
        # An RPC error can interrupt the child's normal stop loop. Quiesce only
        # operations owned by these exclusive leases before releasing them.
        if self.ax and self.last_ax:
            try:self.ax.request('pause',command_id=self.last_ax)
            except Exception:self.progress('翱象暂停回执待核对，保留原命令并等待会话安全释放')
        if self.jst and self.jst_operation:
            try:self.jst.request('sync.stop')
            except Exception:self.progress('仓资料暂停回执待核对，等待会话安全释放')
        self.release_ax();self.release_jst()


class Task(QThread):
    progress=Signal(str)
    succeeded=Signal(object)
    failed=Signal(str)

    def __init__(self,host,operation,arguments,*,bridge_factory=Bridge):
        super().__init__(host)
        self.host,self.operation,self.arguments=host,operation,arguments
        self.ctx=multiprocessing.get_context('spawn');self.cancel=self.ctx.Event()
        self.bridge_factory=bridge_factory;self.pid=None;self.diagnostic=None;self.cancel_reason=''

    def run(self):
        from stockout_worker import serve
        parent,child=self.ctx.Pipe()
        bridge=self.bridge_factory(self.host,self.cancel,self.progress.emit)
        process=self.ctx.Process(target=serve,args=(child,str(self.host.repository.folder),self.operation,self.arguments,self.cancel))
        result=None;error=None;received=False
        try:
            process.start();self.pid=process.pid;child.close()
            while True:
                if bridge.yielding():self.cancel.set()
                if parent.poll(.15):
                    try:kind,value=parent.recv()
                    except EOFError:break
                    if kind=='progress':self.progress.emit(value)
                    elif kind=='rpc':
                        try:parent.send(('rpc_result',bridge.request(value)))
                        except Exception as exc:parent.send(('rpc_error',str(exc)))
                    elif kind=='success':result=value;received=True
                    elif kind=='diagnostic':self.diagnostic=value
                    elif kind=='error':
                        import logging
                        logging.log(logging.INFO if value['type']=='Cancelled' else logging.ERROR,
                                    '门店缺货率 %s; 取消来源=%s: %s',self.operation,self.cancel_reason or '未由页面请求',value['traceback'])
                        error=self.cancel_reason or value['message'] if value['type']=='Cancelled' else value['message'];received=True
                    if received:break  # Windows closed pipe can raise WinError 109 on a later poll.
                elif not process.is_alive():break
            process.join()
            if not received:error='分析工作进程异常退出；原任务编号和已确认原件保留，请继续原任务核对'
        except Exception as exc:error=str(exc)
        finally:
            self.cancel.set()
            parent.close();child.close()
            if process.pid is not None and process.is_alive():
                # Cooperative completion only. No terminate/kill during commits.
                self.progress.emit('正在等待分析安全退出')
                process.join()
            try:bridge.close()
            except Exception as exc:error=(error+'；' if error else '')+'共享会话释放需核对：'+str(exc)
            if process.pid is not None:process.close()
        if error:self.failed.emit(error)
        else:self.succeeded.emit(result)
