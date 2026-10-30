"""Modal progress keeps the UI event loop alive while an owned process handles large work I/O."""
import multiprocessing,time
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
        progress('传输本次操作所需数据…')
        send_tree(self.connection,value,None if self.writing else self.cancel,lambda n:progress(f'正在传输数据：已发送 {n:,} 个数据块（总量未预扫描）'))
        while True:
            if not self.writing and operation!='storage_copy' and self.cancel.is_set():raise Cancelled('已取消')
            if not self.connection.poll(.05):
                if not self.process.is_alive():raise RuntimeError('作业处理进程退出；请在作业记录核对保存状态后重试')
                continue
            kind,data=self.connection.recv()
            if kind=='commit':
                from runtime_support import CommitProgress
                progress(CommitProgress(data))
            elif kind=='progress':progress(data)
            elif kind=='rows':
                from export_progress import RowProgress
                progress(RowProgress(*data))
            elif kind=='error':
                self.writing=False
                if data[0]=='Cancelled':raise Cancelled(data[1])
                raise RuntimeError(data[1])
            elif kind=='success':
                result=receive_tree(self.connection,None if self.writing or operation=='storage_copy' else self.cancel,lambda n:progress(f'正在接收数据：已接收 {n:,} 个数据块（总量未预扫描）'),alive=self.process.is_alive)[0];self.writing=False;return result
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
    progress=Signal(str);rows=Signal(object,object);commit=Signal(bool)
    def __init__(self,session,operation,value,parent):super().__init__(parent);self.session=session;self.operation=operation;self.value=value;self.result=None;self.error=None
    def run(self):
        try:
            last=[None,0.0]
            def progress(message):
                if hasattr(message,'active'):self.commit.emit(message.active)
                if hasattr(message,'current') and hasattr(message,'total'):
                    key=(str(message).split('：',1)[0],message.total);now=time.monotonic()
                    if key==last[0] and now-last[1]<.1 and message.current not in (0,message.total):return
                    last[:]=[key,now]
                else:last[0]=None
                self.progress.emit(str(message))
                self.rows.emit(message.current,message.total) if hasattr(message,'current') and hasattr(message,'total') else self.rows.emit(0,0)
            self.result=self.session.call(self.operation,self.value,progress)
        except Exception as exc:self.error=exc
        finally:self.value=None


class Progress(QDialog):
    def __init__(self,parent,session,operation,value):
        super().__init__(parent);self.session=session;self.operation=operation;self._completed=False;self.setWindowTitle({'prepare_save':'核对保存','save':'保存作业','prepare_normal':'核对归档','confirm_normal':'确认归档','delivery_prepare':'核对导出','delivery_execute':'导出确认单','review_export':'保存完整差异','copy_config':'复制配置','storage_copy':'创建兼容存储副本'}.get(operation,'读取作业'));self.setMinimumWidth(510)
        self.started=time.monotonic();self.last_progress=self.started;self.cancelling=False;self.committing=False;self.cancel_after_commit=False
        self.setWindowTitle({'delivery_probe':'核对导出','delivery_expand':'准备完整签发草稿'}.get(operation,self.windowTitle()))
        v=QVBoxLayout(self);self.note=label('正在准备，请稍候…','muted');v.addWidget(self.note);self.bar=QProgressBar();self.bar.setRange(0,0);v.addWidget(self.bar)
        self.health=label('','muted');v.addWidget(self.health)
        self.cancel_btn=button('取消',self.reject);self.cancel_btn.setEnabled(operation not in ('save','review_export'));v.addWidget(self.cancel_btn)
        self.task=Request(session,operation,value,self);self.task.commit.connect(self.commit_state);self.task.progress.connect(self.update_stage);self.task.rows.connect(self.update_count);self.task.finished.connect(self.completed)
        self.clock=QTimer(self);self.clock.setInterval(500);self.clock.timeout.connect(self.update_health);self.clock.start();self.update_health()
    def commit_state(self,active):
        self.committing=active;self.cancel_btn.setEnabled(not active and not self.cancelling)
        if not active and self.cancel_after_commit:self.reject()
    def update_stage(self,text):
        self.last_progress=time.monotonic()
        if not self.cancelling:self.note.setText(text)
    def update_count(self,n,total):
        if total:
            self.bar.setRange(0,1000);self.bar.setValue(min(1000,int(n*1000/total)));self.bar.setFormat('当前阶段 %p%')
        else:self.bar.setRange(0,0)
    def update_health(self):
        now=time.monotonic();elapsed=int(now-self.started);quiet=int(now-self.last_progress)
        alive=self.session.process.is_alive()
        status=('进程仍存活，当前阶段暂无新进展' if quiet>=10 else '处理中') if alive else '处理进程已退出，正在核对结果'
        self.health.setText(f'已用时 {elapsed//60:02d}:{elapsed%60:02d} · 最近进展 {quiet} 秒前 · {status}')
    def reject(self):
        if self._completed:super().reject();return
        if self.committing:self.cancel_after_commit=True;self.note.setText('最终提交已开始，完成后停止后续操作。');return
        if self.operation in ('save','review_export'):self.note.setText('正在完成本次提交，结束后可关闭；不会中途终止写入。');return
        self.cancelling=True;self.session.cancel.set();self.cancel_btn.setEnabled(False);self.note.setText('正在取消；最终提交前会回滚，已提交归档及已完成文件会保留…' if self.operation in ('delivery_execute','confirm_normal') else '正在取消准备，原记录不会改变…')
    def closeEvent(self,event):
        if self._completed:event.accept()
        else:self.reject();event.ignore()
    def completed(self):
        self._completed=True;self.clock.stop()
        if self.task.error is None:self.bar.setRange(0,1000);self.bar.setValue(1000);self.note.setText('操作完成')
        self.accept()


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
