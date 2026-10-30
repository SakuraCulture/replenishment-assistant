"""Thread workers and shared task lifecycle, with explicit host UI callbacks."""
import logging,threading
from PySide6.QtCore import Signal,QThread
from PySide6.QtWidgets import QMessageBox
import engine,batch_service
from export_progress import RowProgress

class Worker(QThread):
    status=Signal(str);row_progress=Signal(int,int);success=Signal(object);failed=Signal(str);cancelled=Signal()
    def __init__(self,fn,args,parent):super().__init__(parent);self.fn=fn;self.args=args;self.cancel=threading.Event()
    def run(self):
        try:
            def report(message):
                self.row_progress.emit(message.current,message.total) if isinstance(message,RowProgress) else self.row_progress.emit(0,0)
                self.status.emit(str(message))
            result=self.fn(*self.args,progress=report,cancel=self.cancel)
            owner=self.parent()
            if hasattr(owner,'work_repository'):
                from source_archive import retained_import
                retained_import(self.fn,self.args,result,owner.work_repository.path,report,self.cancel)
            if self.fn is batch_service.load:engine.checkpoint(self.cancel)
            self.success.emit(result)
        except engine.Cancelled:self.cancelled.emit()
        except Exception as e:logging.exception('操作失败');self.failed.emit(str(e))


class BatchWorker(QThread):
    changed=Signal(str,str,object,str);failed=Signal(str)
    def __init__(self,loaded,jobs,profile,workers,parent):
        super().__init__(parent);self.loaded=loaded;self.jobs=jobs;self.profile=profile;self.workers=workers;self.cancel=threading.Event()
    def run(self):
        try:batch_service.run(self.loaded,self.jobs,self.profile,self.workers,self.changed.emit,self.cancel)
        except Exception as exc:self.failed.emit(str(exc))


class BackgroundJobs:
    def job(self,fn,args,success):
        if self.worker:return
        w=Worker(fn,args,self);self.worker=w;self.progress.setRange(0,0);self.update_controls()
        if fn is batch_service.load:
            # Queued check-file signals may arrive after cancellation. Other task flows stay unchanged.
            def active():return self.worker is w and not w.cancel.is_set() and not self.closing
            w.status.connect(lambda message:self.status.setText(message) if active() else None)
            w.success.connect(lambda result:success(result) if active() else None)
            w.failed.connect(lambda message:self.failed(message) if active() else None)
        else:
            w.status.connect(self.status.setText);w.success.connect(success);w.failed.connect(self.failed)
        def rows(current,total):
            if self.worker is not w or self.closing:return
            self.progress.setRange(0,max(total,1) if total or current else 0);self.progress.setValue(current)
        w.row_progress.connect(rows)
        w.cancelled.connect(lambda:self.status.setText('操作已停止。分店导出时已完成文件会保留，请查看目标目录中的部分导出说明。'))
        w.finished.connect(self.job_finished);w.start()

    def job_finished(self):
        w=self.worker;self.worker=None
        if w:w.deleteLater()
        self.update_controls()
        if self.closing:self.close()

    def failed(self,message):
        self.status.setText('未完成：'+message)
        QMessageBox.warning(self,'操作未完成',message)

    def cancel_work(self):
        if self.worker:self.worker.cancel.set();self.status.setText('正在取消，当前读取步骤结束后返回…')

    def closeEvent(self,event):
        if self.worker:self.closing=True;self.cancel_work();event.ignore()
        else:event.accept()

