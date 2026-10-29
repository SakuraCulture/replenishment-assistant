"""Modal progress keeps the UI event loop alive while an owned process handles large work I/O."""
import multiprocessing
from PySide6.QtCore import QThread,Signal,QTimer
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QProgressBar
from ui_controls import label,button
from runtime_support import Cancelled
from work_transfer import send_tree,receive_tree
from work_io_process import serve


class Session:
    def __init__(self,path):
        self.context=multiprocessing.get_context('spawn');self.cancel=self.context.Event();self.connection,child=self.context.Pipe()
        self.process=self.context.Process(target=serve,args=(child,str(path),self.cancel));self.process.start();child.close();self.writing=False
    def call(self,operation,value,progress):
        self.writing=operation in ('save','confirm_normal','delivery_execute','review_export');self.connection.send(operation)
        send_tree(self.connection,value,None if self.writing else self.cancel,lambda n:progress('正在准备完整作业快照…'))
        while True:
            if not self.writing and self.cancel.is_set():raise Cancelled('已取消')
            if not self.connection.poll(.05):
                if not self.process.is_alive():raise RuntimeError('作业处理进程退出；请在作业记录核对保存状态后重试')
                continue
            kind,data=self.connection.recv()
            if kind=='progress':progress(data)
            elif kind=='rows':
                from export_progress import RowProgress
                progress(RowProgress(*data))
            elif kind=='error':
                self.writing=False
                if data[0]=='Cancelled':raise Cancelled(data[1])
                raise RuntimeError(data[1])
            elif kind=='success':
                result=receive_tree(self.connection,None if self.writing else self.cancel,lambda n:progress('正在加载完整作业快照…'))[0];self.writing=False;return result
            else:raise RuntimeError('作业处理进程返回未知消息')
    def close(self):
        # All write calls finish before their modal dialog closes. Termination is
        # reserved for read/transfer cancellation, never an in-flight transaction.
        try:self.connection.send(None)
        except (OSError,EOFError,BrokenPipeError):pass
        self.process.join(.15)
        if self.process.is_alive():
            if self.writing:self.process.join()
            else:self.process.terminate();self.process.join(2)
        self.connection.close();self.process.close()
    def __enter__(self):return self
    def __exit__(self,*_):self.close()


class Request(QThread):
    progress=Signal(str);rows=Signal(int,int)
    def __init__(self,session,operation,value,parent):super().__init__(parent);self.session=session;self.operation=operation;self.value=value;self.result=None;self.error=None
    def run(self):
        try:
            def progress(message):
                self.progress.emit(str(message))
                self.rows.emit(message.current,message.total) if hasattr(message,'current') and hasattr(message,'total') else self.rows.emit(0,0)
            self.result=self.session.call(self.operation,self.value,progress)
        except Exception as exc:self.error=exc
        finally:self.value=None


class Progress(QDialog):
    def __init__(self,parent,session,operation,value):
        super().__init__(parent);self.session=session;self.operation=operation;self._completed=False;self.setWindowTitle({'prepare_save':'核对保存','save':'保存作业','prepare_normal':'核对归档','confirm_normal':'确认归档','delivery_prepare':'核对导出','delivery_execute':'导出确认单','review_export':'保存完整差异','copy_config':'复制配置'}.get(operation,'读取作业'));self.setMinimumWidth(510)
        v=QVBoxLayout(self);self.note=label('正在准备，请稍候…','muted');v.addWidget(self.note);bar=QProgressBar();bar.setRange(0,0);v.addWidget(bar)
        self.cancel_btn=button('取消',self.reject);self.cancel_btn.setEnabled(operation not in ('save','confirm_normal','review_export'));v.addWidget(self.cancel_btn)
        self.task=Request(session,operation,value,self);self.task.progress.connect(self.note.setText);self.task.rows.connect(lambda n,total:(bar.setRange(0,max(total,1) if total or n else 0),bar.setValue(n)));self.task.finished.connect(self.completed)
    def reject(self):
        if self._completed:super().reject();return
        if self.operation in ('save','confirm_normal','review_export'):self.note.setText('正在完成本次提交，结束后可关闭；不会中途终止写入。');return
        self.session.cancel.set();self.cancel_btn.setEnabled(False);self.note.setText('正在停止导出；已完成归档和文件会保留，当前事务结束后返回…' if self.operation=='delivery_execute' else '正在取消读取，原记录不会改变…')
    def closeEvent(self,event):
        if self._completed:event.accept()
        else:self.reject();event.ignore()
    def completed(self):self._completed=True;self.accept()


def request(parent,session,operation,value):
    host=parent
    while host is not None and not hasattr(host,'work_repository'):host=host.parentWidget()
    session.cancel.clear();dialog=Progress(parent,session,operation,value)
    previous=getattr(host,'_large_io_dialog',None) if host else None
    if host:host._large_io_dialog=dialog
    try:
        dialog.task.start();dialog.exec();dialog.task.wait();result=dialog.task.result;error=dialog.task.error
    finally:
        if host:host._large_io_dialog=previous
        dialog.deleteLater()
    if host and host.closing:QTimer.singleShot(0,host.close)
    if error:raise error
    return result


def read_job(parent,path,day,copy_config=False):
    with Session(path) as session:return request(parent,session,'copy_config' if copy_config else 'load',day)
