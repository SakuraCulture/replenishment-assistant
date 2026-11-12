"""One bounded background consumer per window; no modal post-import barrier."""
from PySide6.QtCore import QObject,QTimer
from PySide6.QtWidgets import QHBoxLayout,QProgressBar,QApplication
from ui_controls import button,label
from ui_tasks import Worker
import jst_tasks


class Generator(QObject):
    def __init__(self,host,layout):
        super().__init__(host);self.host=host;self.worker=None;self.committing=False;self.stopped=False;self.active_work=False
        row=QHBoxLayout();self.note=label('回传确认后进入待分配；准备本批订单和库存后自动连续分配，不自动确认付款。','muted')
        row.addWidget(self.note,1);self.bar=QProgressBar();self.bar.setMaximumWidth(180);self.bar.setRange(0,1);row.addWidget(self.bar)
        self.stop=button('停止后续生成',self.cancel);self.resume=button('选择批次继续',lambda:host.followup.open_stage('jst'));self.stop.setEnabled(False)
        row.addWidget(self.stop);row.addWidget(self.resume)
        row.addWidget(button('查看分配结果',lambda:host.followup.open_stage('jst')));layout.addLayout(row)
        self.timer=QTimer(self);self.timer.setInterval(3000);self.timer.timeout.connect(self.tick);self.timer.start()
        QTimer.singleShot(500,self.tick)
    def start(self):self.stopped=False;self.tick()
    def tick(self):
        h=self.host
        modal=QApplication.activeModalWidget();io=getattr(h,'_large_io_dialog',None)
        if self.worker or self.stopped or h.closing or getattr(h,'_data_location_preparing',False) or h.worker or (io and not io.property('jstResults')) or (modal and not modal.property('jstResults')):return
        self.active_work=False
        w=Worker(self.drain,(h.work_repository.path,h.repository.folder),self);self.worker=w
        w.status.connect(self.note.setText);w.row_progress.connect(self.progress);w.commit_state.connect(self.boundary)
        w.success.connect(self.ready);w.failed.connect(lambda s:self.note.setText('回传已保存；生成未完成：'+s))
        w.cancelled.connect(lambda:self.note.setText('自动生成已停止；已保存单保留，未完成可继续。'))
        w.finished.connect(self.finished);self.stop.setEnabled(True);self.resume.setEnabled(False);w.start()
    def drain(self,path,folder,progress,cancel):
        # Empty periodic queue checks are not an executing allocation task.
        # This plain state flag is set before the first real batch operation;
        # no widget is accessed from the worker thread.
        def before_work():self.active_work=True
        return jst_tasks.drain(path,folder,progress,cancel,before_work=before_work)
    def progress(self,n,total):self.bar.show();self.bar.setRange(0,max(total,1) if total else 0);self.bar.setValue(n)
    def boundary(self,active):self.committing=active;self.stop.setEnabled(not active and not self.stopped)
    def cancel(self):
        self.stopped=True
        if self.worker:self.worker.cancel.set()
        self.stop.setEnabled(False);self.note.setText('正在停止；当前原子提交完成后退出，已生成文件可继续导出。')
    def ready(self,rows):
        if not rows:return
        errors=[r['error'] for r in rows if r.get('error')]+[e['error'] for r in rows for e in r.get('errors',[])]
        count=sum(len(r.get('generated',[]))+len(r.get('zero',[])) for r in rows)
        self.note.setText(f'本轮已保存{count}单；'+('已暂停，请核对：'+'；'.join(errors) if errors else '可在分配结果页导出。'))
        self.host.followup.query(keep_page=True)
    def finished(self):
        w=self.worker;self.worker=None;self.committing=False;self.active_work=False;w.deleteLater();self.bar.setVisible(False)
        self.stop.setEnabled(False);self.resume.setEnabled(True)
        if self.host.closing:QTimer.singleShot(0,self.host.close)
