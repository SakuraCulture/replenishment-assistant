"""One persistent read-only spawn worker, one running and one latest pending request."""
import multiprocessing as mp
from PySide6.QtCore import QObject,QTimer,Signal
from history_read import worker_main

class HistoryReader(QObject):
    def __init__(self,path,cache,parent=None):
        super().__init__(parent);self.path=str(path);self.cache=str(cache);self.generation=0;self.active=None;self.pending=None;self.proc=None;self.conn=None;self.cancel_event=None;self.callbacks={};self.timer=QTimer(self);self.timer.timeout.connect(self.poll)
    def request(self,request,done,error,progress=lambda p:None):
        self.generation+=1;token=self.generation;self.callbacks={token:(done,error,progress)};self.pending=(token,request)
        if self.active:self.cancel_event.set()
        else:self.dispatch()
        return token
    def dispatch(self):
        if self.pending is None:return
        if self.proc is None:
            ctx=mp.get_context('spawn');self.conn,child=ctx.Pipe();self.cancel_event=ctx.Event();self.proc=ctx.Process(target=worker_main,args=(child,self.cancel_event,self.path,self.cache),daemon=True);self.proc.start();child.close()
        self.cancel_event.clear();self.active=self.pending;self.pending=None;self.conn.send(self.active);self.timer.start(20)
    def poll(self):
        if self.proc is None:return
        try:
            while self.conn is not None and self.conn.poll():
                token,kind,value=self.conn.recv();callbacks=self.callbacks.get(token)
                if kind=='progress':
                    if callbacks:callbacks[2](value)
                    continue
                self.active=None
                if callbacks:
                    self.callbacks.pop(token,None)
                    (callbacks[0] if kind=='done' else callbacks[1])(value)
                self.dispatch()
            if self.proc is not None and not self.proc.is_alive():raise RuntimeError('历史读取进程异常退出，请重试')
        except (EOFError,OSError,RuntimeError) as exc:
            callbacks=list(self.callbacks.values());self.shutdown()
            for cb in callbacks:cb[1](str(exc))
    def cancel(self):
        self.generation+=1;self.pending=None;self.callbacks.clear()
        if self.active:self.cancel_event.set()
    def shutdown(self):
        self.cancel();self.timer.stop()
        if self.proc is not None:
            # Worker reads source only. Stop IPC first, terminate only this owned
            # worker if a noninterruptible JSON read has not reached a boundary.
            try:self.conn.send(None)
            except (OSError,EOFError):pass
            self.proc.join(.15)
            if self.proc.is_alive():self.proc.terminate();self.proc.join(2)
            self.conn.close();self.proc.close()
        self.proc=None;self.conn=None;self.active=None
# Display-only status hashes. Results are replaced by workbench actions; this task
# retains each old reference until finished, then generation validation discards it.
# It never supplies a digest to signing or export.
import hashlib,json
from PySide6.QtCore import QThread
import work_repository as wr
import new_product_focus as focus

class StatusHashes(QThread):
    completed=Signal(object)
    def __init__(self,items,new_focus,parent=None):
        super().__init__(parent);self.items=items;self.focus=new_focus
    def run(self):
        result={}
        for store,tree in self.items.items():
            business={k:v for k,v in tree.items() if k not in ('created','task','version')}
            if self.focus is not None:business={'result':business,'new_focus':focus.validate(self.focus)['revision']}
            digest=hashlib.sha256();encoder=json.JSONEncoder(ensure_ascii=False,sort_keys=True,allow_nan=False,default=wr.encode_default)
            for i,chunk in enumerate(encoder.iterencode(business)):
                if i%2048==0 and self.isInterruptionRequested():return
                digest.update(chunk.encode())
            result[store]=digest.hexdigest()
        self.completed.emit(result)
