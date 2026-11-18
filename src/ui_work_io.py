"""Modal progress keeps the UI event loop alive while an owned process handles large work I/O."""
import multiprocessing,threading,time
import performance_log
from PySide6.QtCore import QThread,Signal,QTimer
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QProgressBar
from ui_controls import label,button
from runtime_support import Cancelled,CommitProgress
from work_transfer import TransferClosed,send_tree,receive_tree
from work_io_process import serve
from work_failures import FailureContext,WorkFailure,format_failure


READ_OPERATIONS = frozenset(('prepare_save','prepare_normal','delivery_probe','delivery_prepare',
                             'delivery_expand','load','copy_config','delete_preview'))
READ_STALL_SECONDS = 180
READ_CANCEL_GRACE = 1.5


class Session:
    def __init__(self,path,*,worker=serve,stall_seconds=READ_STALL_SECONDS):
        self.context=multiprocessing.get_context('spawn');self.cancel=self.context.Event();self.connection,child=self.context.Pipe()
        self.path=path;self.stall_seconds=stall_seconds;self.writing=False;self.broken=False
        self.process=self.context.Process(target=worker,args=(child,str(path),self.cancel))
        try:self.process.start()
        except BaseException:self.connection.close();raise
        finally:child.close()
        performance_log.owned(self.process,'work-io')

    def _watch_read(self,state,finished):
        # Pipe writes/reads can block in the OS. Only an owned read-only child
        # may be interrupted here; closing its end releases the blocking call.
        cancel_at=None;counter=None;previous=None
        while not finished.wait(.2) and self.process.is_alive():
            now=time.monotonic();last=state['activity']
            if self.cancel.is_set():
                if cancel_at is None:cancel_at=now
                if now-cancel_at<READ_CANCEL_GRACE:continue
                reason='cancelled'
            elif now-last<self.stall_seconds:
                previous=None;continue
            else:
                if counter is None:
                    from performance_counters import Counters
                    counter=Counters()
                sample=counter.process(self.process.pid,self.process.sentinel)
                activity=tuple(sample.get(k) for k in ('identity','cpu_seconds','read_bytes','write_bytes'))
                if activity[0] is None and state['phase']=='waiting':continue
                if previous is None or previous[:2]!=(last,activity):
                    previous=(last,activity,now);continue
                if now-previous[2]<2:continue
                reason='stalled'
            if finished.is_set() or (reason=='stalled' and state['activity']!=last):continue
            state['abort']=reason
            try:self.process.terminate()
            except OSError:
                state['abort']=None
                if self.process.is_alive():continue
            return

    @performance_log.work_io
    def call(self,operation,value,progress):
        if self.broken:raise RuntimeError('原作业连接已结束，请重新发起操作')
        self.writing=operation in ('save','confirm_normal','delivery_execute','review_export','storage_copy','delete_orders','delete_job')
        state={'activity':time.monotonic(),'phase':'sending','abort':None}
        context=FailureContext(operation)
        if self.writing:context.commit_state='unknown'
        finished=threading.Event();watcher=None
        if operation in READ_OPERATIONS:
            watcher=threading.Thread(target=self._watch_read,args=(state,finished),daemon=True)
            watcher.start()
        def activity():state['activity']=time.monotonic()
        def report(message):
            context.stage=str(message);activity();progress(message)
        try:
            report('正在发送作业操作…');self.connection.send(operation)
            report('传输本次操作所需数据…')
            send_tree(self.connection,value,None if self.writing else self.cancel,
                      lambda n:report(f'正在传输数据：已发送 {n:,} 个数据块（总量未预扫描）'),activity=activity)
            state['phase']='waiting';report('等待作业处理进度…')
            while True:
                if not self.writing and self.cancel.is_set():raise Cancelled('已取消')
                if not self.connection.poll(.05):
                    if not self.process.is_alive():raise TransferClosed('作业处理进程已退出')
                    continue
                kind,data=self.connection.recv();activity()
                if kind=='commit':
                    context.commit_state='unknown' if data else 'committed';report(CommitProgress(data))
                elif kind=='receipt':context.export_task=data
                elif kind=='progress':report(data)
                elif kind=='rows':
                    from export_progress import RowProgress
                    report(RowProgress(*data))
                elif kind=='error':
                    self.writing=False
                    if isinstance(data,dict):raise WorkFailure(data['evidence'],data['message'])
                    if data[0]=='Cancelled':raise Cancelled(data[1])
                    raise RuntimeError(data[1])
                elif kind=='success':
                    state['phase']='receiving';report('正在接收作业结果…')
                    result=receive_tree(self.connection,None if self.writing else self.cancel,
                                        lambda n:report(f'正在接收数据：已接收 {n:,} 个数据块（总量未预扫描）'),
                                        alive=self.process.is_alive,activity=activity)[0]
                    self.writing=False
                    return result
                else:raise TransferClosed('作业处理进程返回未知消息')
        except Cancelled:
            self.broken=True;raise
        except (EOFError,OSError,TransferClosed,MemoryError) as exc:
            self.broken=True
            if state['abort']=='cancelled':raise Cancelled('已取消本次只读准备') from exc
            self.process.join(.15)
            context.transport={'phase':state['phase'],'worker_pid':self.process.pid,'worker_exitcode':self.process.exitcode,
                               'seconds_without_progress':round(time.monotonic()-state['activity'],3)}
            if state['abort']=='stalled':
                cause=TimeoutError('只读作业长时间无进展，已结束本次准备，可重新核对')
            else:
                cause=exc
            evidence=context.capture(cause,self.path)
            message=format_failure(evidence)
            if self.writing and not context.export_task:
                message+='\n请在订单跟踪核对本次归档和导出任务后再继续。'
            raise WorkFailure(evidence,message) from exc
        finally:
            finished.set()
            if watcher is not None:watcher.join()
    def close(self):
        # All write calls finish before their modal dialog closes. Termination is
        # reserved for read/transfer cancellation, never an in-flight transaction.
        if self.broken:self.connection.close()
        elif self.process.is_alive():
            try:self.connection.send(None)
            except (OSError,EOFError):pass
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
        super().__init__(parent);self.session=session;self.operation=operation;self._completed=False;self.setWindowTitle({'prepare_save':'核对保存','save':'保存作业','prepare_normal':'核对归档','confirm_normal':'确认归档','delivery_prepare':'核对导出','delivery_execute':'导出确认单','review_export':'保存完整差异','copy_config':'复制配置','storage_copy':'创建数据库备份'}.get(operation,'读取作业'));self.setMinimumWidth(510)
        self.started=time.monotonic();self.last_progress=self.started;self.cancelling=False;self.committing=False;self.cancel_after_commit=False;self.committed=False;self.exporting=False
        self.setWindowTitle({'delivery_probe':'核对导出','delivery_expand':'准备完整签发草稿'}.get(operation,self.windowTitle()))
        self.setWindowTitle({'delete_preview':'核对彻底删除范围','delete_orders':'彻底删除关联数据','delete_job':'删除每日草稿'}.get(operation,self.windowTitle()))
        v=QVBoxLayout(self);self.note=label('正在准备，请稍候…','muted');v.addWidget(self.note);self.bar=QProgressBar();self.bar.setRange(0,0);v.addWidget(self.bar)
        self.health=label('','muted');v.addWidget(self.health)
        self.cancel_btn=button('正在保存，暂不能取消' if operation in ('save','review_export') else '取消准备',self.reject);self.cancel_btn.setEnabled(operation not in ('save','review_export'));v.addWidget(self.cancel_btn)
        if operation in ('delete_orders','delete_job'):self.cancel_btn.setText('取消删除')
        self.task=Request(session,operation,value,self);self.task.commit.connect(self.commit_state);self.task.progress.connect(self.update_stage);self.task.rows.connect(self.update_count);self.task.finished.connect(self.completed)
        self.clock=QTimer(self);self.clock.setInterval(500);self.clock.timeout.connect(self.update_health);self.clock.start();self.update_health()
    def commit_state(self,active):
        self.committed=self.committed or (self.committing and not active);self.committing=active;self.cancel_btn.setEnabled(not active and not self.cancelling and self.operation not in ('save','review_export'));self.cancel_btn.setText('正在提交，暂不能取消' if active or self.operation in ('save','review_export') else '停止剩余导出' if self.operation=='delivery_execute' and self.committed else '取消准备')
        if not active and self.cancel_after_commit:self.reject()
    def update_stage(self,text):
        self.last_progress=time.monotonic()
        if self.operation in ('delete_orders','delete_job') and text=='归档已提交；可取消后续导出':text='正在核对删除结果…'
        if self.operation=='delivery_execute' and '写出确认单' in text:
            self.exporting=True;self.cancel_btn.setText('停止剩余导出')
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
        self.cancelling=True;self.session.cancel.set();self.cancel_btn.setEnabled(False);self.cancel_btn.setText('正在停止…');self.note.setText('正在停止后续导出；原签发修订及已完成文件保留，未完成项可从历史按原修订重试。' if self.committed or self.exporting else '正在取消准备；提交前会回滚。备份或缓存可能已产生，原正式记录保留。')
        if self.operation in ('delete_orders','delete_job'):self.note.setText('删除已经提交，正在接收完成结果。' if self.committed else '正在取消删除，已进入事务的变更会整体回滚；请等待安全结束。')
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
