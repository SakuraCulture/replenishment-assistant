"""Thread workers and shared task lifecycle, with explicit host UI callbacks."""
import logging,threading
from PySide6.QtCore import Signal,QThread
from PySide6.QtWidgets import QMessageBox
import engine,batch_service

class Worker(QThread):
    status=Signal(str);success=Signal(object);failed=Signal(str);cancelled=Signal()
    def __init__(self,fn,args,parent):super().__init__(parent);self.fn=fn;self.args=args;self.cancel=threading.Event()
    def run(self):
        try:self.success.emit(self.fn(*self.args,progress=self.status.emit,cancel=self.cancel))
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
        w=Worker(fn,args,self);self.worker=w;self.update_controls();w.status.connect(self.status.setText);w.success.connect(success);w.failed.connect(self.failed);w.cancelled.connect(lambda:self.status.setText('操作已停止。分店导出时已完成文件会保留，请查看目标目录中的部分导出说明。'));w.finished.connect(self.job_finished);w.start()

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

