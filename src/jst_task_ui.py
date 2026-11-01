"""One bounded background consumer per window; no modal post-import barrier."""
from PySide6.QtCore import QObject,QTimer
from PySide6.QtWidgets import QHBoxLayout,QProgressBar,QApplication
from ui_controls import button,label
from ui_tasks import Worker
import jst_tasks


class Generator(QObject):
    def __init__(self,host,layout):
        super().__init__(host);self.host=host;self.worker=None;self.committing=False;self.stopped=False
        row=QHBoxLayout();self.note=label('回传确认后自动生成聚水潭导入单；不自动确认付款。','muted')
        row.addWidget(self.note,1);self.bar=QProgressBar();self.bar.setMaximumWidth(180);self.bar.setRange(0,1);row.addWidget(self.bar)
        self.stop=button('停止后续生成',self.cancel);self.resume=button('继续生成',self.start);self.stop.setEnabled(False)
        row.addWidget(self.stop);row.addWidget(self.resume);layout.addLayout(row)
        self.timer=QTimer(self);self.timer.setInterval(3000);self.timer.timeout.connect(self.tick);self.timer.start()
        QTimer.singleShot(500,self.tick)
    def start(self):self.stopped=False;self.tick()
    def tick(self):
        h=self.host
        if self.worker or self.stopped or h.closing or h.worker or getattr(h,'_large_io_dialog',None) or QApplication.activeModalWidget():return
        w=Worker(jst_tasks.drain,(h.work_repository.path,h.repository.folder),self);self.worker=w
        w.status.connect(self.note.setText);w.row_progress.connect(self.progress);w.commit_state.connect(self.boundary)
        w.success.connect(self.ready);w.failed.connect(lambda s:self.note.setText('回传已保存；生成未完成：'+s))
        w.cancelled.connect(lambda:self.note.setText('自动生成已停止；已保存单保留，未完成可继续。'))
        w.finished.connect(self.finished);self.stop.setEnabled(True);self.resume.setEnabled(False);w.start()
    def progress(self,n,total):self.bar.setRange(0,max(total,1) if total else 0);self.bar.setValue(n)
    def boundary(self,active):self.committing=active;self.stop.setEnabled(not active and not self.stopped)
    def cancel(self):
        self.stopped=True
        if self.worker:self.worker.cancel.set()
        self.stop.setEnabled(False);self.note.setText('正在停止；当前原子提交完成后退出，已生成文件可继续导出。')
    def ready(self,rows):
        if not rows:return
        errors=[r['number']+'：'+r['error'] for r in rows if r.get('error')]
        self.note.setText(f'回传后生成处理{len(rows)}单；'+('待处理：'+'；'.join(errors) if errors else '已完成，可在聚水潭导入单面板导出。'))
        self.host.followup.query(keep_page=True)
    def finished(self):
        w=self.worker;self.worker=None;self.committing=False;w.deleteLater();self.bar.setRange(0,1);self.bar.setValue(1)
        self.stop.setEnabled(False);self.resume.setEnabled(True)
        if self.host.closing:QTimer.singleShot(0,self.host.close)
