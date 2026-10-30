"""Reusable read-only business grids and cancellable background operations."""
import time
from PySide6.QtCore import Qt,QAbstractTableModel,QModelIndex,QTimer
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QTableView,QLineEdit,QProgressBar,QAbstractItemView
from ui_controls import label,button
from ui_tasks import Worker
from runtime_support import Cancelled

class GridModel(QAbstractTableModel):
    def __init__(self,headers,rows=(),parent=None):super().__init__(parent);self.headers=headers;self.replace(rows)
    def replace(self,rows):
        self.beginResetModel();self.rows=list(rows);self.tokens=[' '.join(str(v) for v in r).lower() for r in self.rows];self.indices=list(range(len(self.rows)));self.endResetModel()
    def filter(self,text):
        terms=text.lower().split()
        self.beginResetModel();self.indices=[i for i,t in enumerate(self.tokens) if all(term in t for term in terms)];self.endResetModel()
    def rowCount(self,parent=QModelIndex()):return 0 if parent.isValid() else len(self.indices)
    def columnCount(self,parent=QModelIndex()):return 0 if parent.isValid() else len(self.headers)
    def data(self,index,role=Qt.ItemDataRole.DisplayRole):
        if index.isValid() and role in (Qt.ItemDataRole.DisplayRole,Qt.ItemDataRole.ToolTipRole):
            v=self.rows[self.indices[index.row()]][index.column()];return '未确认' if v is None else str(v)
    def headerData(self,i,orientation,role=Qt.ItemDataRole.DisplayRole):
        if role==Qt.ItemDataRole.DisplayRole:return self.headers[i] if orientation==Qt.Orientation.Horizontal else self.indices[i]+1

def grid(parent,headers,rows=()):
    model=GridModel(headers,rows,parent);table=QTableView(parent);table.setModel(model);table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers);table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    for i in range(len(headers)):table.setColumnWidth(i,200 if i else 240)
    return table,model

class Task(QDialog):
    def __init__(self,parent,fn,args,title,cancellable=True):
        super().__init__(parent);self.setWindowTitle(title);self.resize(610,190);self.complete=False;self._completed=False;self.result=None;self.error=None;self.started=time.monotonic();self.cancellable=cancellable
        v=QVBoxLayout(self);self.note=label('准备中…');v.addWidget(self.note);self.bar=QProgressBar();self.bar.setRange(0,0);v.addWidget(self.bar);self.health=label('');v.addWidget(self.health);self.cancel_btn=button('取消',self.reject);self.cancel_btn.setEnabled(cancellable);v.addWidget(self.cancel_btn)
        self.worker=Worker(fn,args,self);self.worker.status.connect(self.note.setText);self.worker.row_progress.connect(self.count);self.worker.success.connect(self.success);self.worker.failed.connect(self.failed);self.worker.cancelled.connect(lambda:self.failed(Cancelled('已取消')));self.worker.finished.connect(self.finish)
        self.clock=QTimer(self);self.clock.setInterval(300);self.clock.timeout.connect(lambda:self.health.setText(f'已用时 {time.monotonic()-self.started:.1f} 秒 · 后台处理中，等待当前阶段完成'));self.clock.start()
    def count(self,n,total):self.bar.setRange(0,max(total,1) if total else 0);self.bar.setValue(n)
    def success(self,result):self.result=result
    def failed(self,message):self.error=message if isinstance(message,Exception) else RuntimeError(message)
    def finish(self):self.complete=True;self._completed=True;self.clock.stop();self.accept()
    def reject(self):
        if self.complete:super().reject()
        elif self.cancellable:self.worker.cancel.set();self.cancel_btn.setEnabled(False);self.note.setText('正在取消，当前安全阶段结束后退出…')
    def closeEvent(self,event):
        if self.complete:event.accept()
        else:self.reject();event.ignore()

def run(parent,fn,args,title,cancellable=True):
    host=parent
    while host is not None and not hasattr(host,'work_repository'):host=host.parentWidget()
    task=Task(parent,fn,args,title,cancellable);previous=getattr(host,'_large_io_dialog',None) if host else None
    if host:host._large_io_dialog=task
    try:
        task.worker.start();task.exec();task.worker.wait();result,error=task.result,task.error
    finally:
        if host:host._large_io_dialog=previous
        task.deleteLater()
    if host and host.closing:QTimer.singleShot(0,host.close)
    if error:raise error
    return result
