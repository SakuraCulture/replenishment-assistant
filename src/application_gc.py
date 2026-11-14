"""Collect Python cycles on the GUI thread while Qt wrappers are live.

CPython's automatic cycle collector can otherwise run in an allocating worker.
Shiboken then queues widget destruction back to the GUI thread. Keep ordinary
reference counting and all worker concurrency, but avoid that cross-thread cycle
finalization path. Explicit gc.collect() in other threads is diagnosed, not
silently monkey-patched.
"""
import gc
import sys
import threading
import time
from PySide6.QtCore import QObject,QTimer,QCoreApplication,QEvent,QThread


class GuiCollector(QObject):
    def __init__(self,app,runtime):
        if threading.current_thread() is not threading.main_thread() or QThread.currentThread()!=app.thread():
            raise RuntimeError('界面回收控制器必须在Qt主线程安装')
        super().__init__(app)
        self.runtime=runtime;self.application=app;self.owner=threading.get_ident();self.enabled_before=gc.isenabled()
        self.stopping=False;self.closed=False;self.busy=False;self.last_full=time.monotonic()
        self.collections=[0,0,0];self.off_thread_collections=0;self.maximum_seconds=0.0
        self.timer=QTimer(self);self.timer.setInterval(250);self.timer.timeout.connect(self.collect_due)
        gc.disable();gc.callbacks.append(self.audit)
        if self.enabled_before:self.timer.start()
        runtime.emit('GUI_GC_INSTALLED',automatic_previously_enabled=self.enabled_before,
                     thresholds=gc.get_threshold(),interval_ms=self.timer.interval())

    def audit(self,phase,info):
        if phase=='start' and not self.closed and threading.get_ident()!=self.owner:
            self.off_thread_collections+=1
            if self.off_thread_collections==1:
                self.runtime.emit('GUI_GC_OFF_THREAD',generation=info.get('generation'),
                    thread=threading.get_ident())

    def collect_due(self):
        if self.closed or self.stopping or self.busy or not self.enabled_before or sys.is_finalizing():return
        if threading.get_ident()!=self.owner:raise RuntimeError('Qt循环回收必须在界面线程执行')
        if self.runtime.phase!='RUNNING':return
        counts=gc.get_count();thresholds=gc.get_threshold()
        if thresholds[0]<=0 or counts[0]<=thresholds[0]:return
        generation=0
        if counts[1]>thresholds[1]:
            generation=1
            if counts[2]>thresholds[2] and time.monotonic()-self.last_full>=10:generation=2
        self.collect(generation)

    def collect(self,generation):
        if threading.get_ident()!=self.owner:raise RuntimeError('Qt循环回收必须在界面线程执行')
        if self.busy:return 0
        self.busy=True;started=time.monotonic()
        try:
            result=gc.collect(generation);self.collections[generation]+=1
            if generation==2:self.last_full=time.monotonic()
            return result
        finally:
            self.maximum_seconds=max(self.maximum_seconds,time.monotonic()-started);self.busy=False

    def drain(self):
        """Called after workers join and before QApplication destruction."""
        if self.closed:return
        if threading.get_ident()!=self.owner:raise RuntimeError('Qt退出回收必须在界面线程执行')
        self.stopping=True
        from shiboken6 import isValid
        if isValid(self.timer):self.timer.stop()
        if not isValid(self.application):return
        self.collect(2)
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)

    def restore(self):
        # No widgets or collection here: QApplication may already be gone.
        if self.closed:return
        if threading.get_ident()!=self.owner:raise RuntimeError('界面回收设置必须在Qt主线程恢复')
        self.closed=True
        from shiboken6 import isValid
        if isValid(self.timer):self.timer.stop()
        if self.audit in gc.callbacks:gc.callbacks.remove(self.audit)
        if self.enabled_before:gc.enable()
        else:gc.disable()
        self.runtime.emit('GUI_GC_RELEASED',collections=self.collections,
            off_thread_collections=self.off_thread_collections,maximum_seconds=self.maximum_seconds,
            automatic_restored=self.enabled_before)
