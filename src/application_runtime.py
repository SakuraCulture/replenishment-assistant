"""Durable, UI-independent shutdown evidence and guarded exception reporting."""
import json,os,sys,threading,traceback
from datetime import datetime,timezone
from pathlib import Path

_runtime=None

def current():return _runtime

def ui_allowed():
    r=_runtime
    if sys.is_finalizing() or r is not None and r.phase!='RUNNING':return False
    try:
        from PySide6.QtCore import QCoreApplication,QThread
        from PySide6.QtWidgets import QApplication
        from shiboken6 import isValid
        app=QApplication.instance()
        return isinstance(app,QApplication) and isValid(app) and not QCoreApplication.closingDown() and QThread.currentThread()==app.thread()
    except (RuntimeError,ImportError,AttributeError):return False

def settings_root(default):
    from data_location import root
    return root(default)

class Runtime:
    def __init__(self,root,*,folder=None):
        global _runtime
        self.root=Path(root).resolve();self.phase='RUNNING';self.errors=0;self.shutdown_errors=0;self.first_shutdown_exception=None;self.request=None;self.lock=threading.RLock()
        self.folder=Path(folder) if folder is not None else self.root.parent/('.'+self.root.name+'.diagnostics');self.folder.mkdir(parents=True,exist_ok=True)
        self.path=self.folder/'application.jsonl';self.previous=sys.excepthook;self.previous_unraisable=sys.unraisablehook;self.previous_thread=threading.excepthook
        _runtime=self;sys.excepthook=self.unexpected;sys.unraisablehook=self.unraisable;threading.excepthook=self.thread_error
        self.emit('STARTED',pid=os.getpid())
    def route(self,folder,*,root=None):
        self.folder=Path(folder);self.folder.mkdir(parents=True,exist_ok=True);self.path=self.folder/'application.jsonl'
        if root is not None:self.root=Path(root)
        self.emit('DIAGNOSTIC_ROUTE')
    def emit(self,event,**values):
        # Open per event: no handle is held in either movable data directory.
        try:
            with self.lock,self.path.open('a',encoding='utf-8') as f:
                f.write(json.dumps({'at':datetime.now(timezone.utc).isoformat(),'pid':os.getpid(),'event':event,'phase':self.phase,**values},ensure_ascii=False)+'\n');f.flush();os.fsync(f.fileno())
        except BaseException:
            try:sys.__stderr__.write('Application diagnostic write failed\n')
            except BaseException:pass
    def transition(self,phase):self.phase=phase;self.emit('PHASE')
    def exception(self,t,v,tb,*,event='EXCEPTION',blocking=True):
        if blocking and self.phase!='RUNNING':
            self.shutdown_errors+=1
            if self.first_shutdown_exception is None:self.first_shutdown_exception=v
        self.errors+=1;self.emit(event,type=getattr(t,'__name__',str(t)),message=str(v),traceback=''.join(traceback.format_exception(t,v,tb)))
    def unexpected(self,t,v,tb):
        self.exception(t,v,tb)
        if ui_allowed():
            try:
                from readable_ui import failure
                failure(None,'操作未完成',f'{t.__name__}: {v}\n诊断位置：{self.path}')
            except BaseException:self.emit('ERROR_DIALOG_FAILED',traceback=traceback.format_exc())
    def unraisable(self,args):self.exception(args.exc_type,args.exc_value,args.exc_traceback,event='UNRAISABLE')
    def thread_error(self,args):self.exception(args.exc_type,args.exc_value,args.exc_traceback,event='THREAD_EXCEPTION')
    def restore(self):
        global _runtime
        # Never leave the QWidget-reporting hook installed into Py_FinalizeEx.
        self.phase='STOPPED';self.emit('STOPPED')
        sys.excepthook=sys.__excepthook__;sys.unraisablehook=self.previous_unraisable;threading.excepthook=self.previous_thread
        _runtime=None

