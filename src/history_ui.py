"""Read-only archive views. Lightweight projections, no full snapshot in Qt."""
import json
from PySide6.QtCore import Qt,QAbstractTableModel,QModelIndex,QTimer
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QComboBox,QLineEdit,QTableView,QFileDialog,QTextBrowser,QMessageBox,QAbstractItemView
from ui_controls import label,button
import work_repository as wr
from history_read import repository

COLUMNS=['商品编码','商品','推荐类型','原模型建议','候选数','签发数','签发金额','原因']

class ArchiveModel(QAbstractTableModel):
    def __init__(self,parent=None):super().__init__(parent);self.rows=[];self.search=[];self.indices=[]
    def replace(self,view):
        self.beginResetModel();self.rows=view['rows'];self.search=view['search'];self.indices=list(range(len(self.rows)));self.endResetModel()
    def filter(self,text):
        self.beginResetModel();self.indices=[i for i,s in enumerate(self.search) if text.lower() in s];self.endResetModel()
    def rowCount(self,parent=QModelIndex()):return 0 if parent.isValid() else len(self.indices)
    def columnCount(self,parent=QModelIndex()):return 0 if parent.isValid() else 8
    def data(self,index,role=Qt.ItemDataRole.DisplayRole):
        if index.isValid() and role in (Qt.ItemDataRole.DisplayRole,Qt.ItemDataRole.ToolTipRole):return self.rows[self.indices[index.row()]][index.column()]
    def headerData(self,s,orientation,role=Qt.ItemDataRole.DisplayRole):
        if role==Qt.ItemDataRole.DisplayRole:return COLUMNS[s] if orientation==Qt.Orientation.Horizontal else self.indices[s]+1


class ArchiveDialog(QDialog):
    def __init__(self,host,record):
        super().__init__(host);self.host=host;self.record=record;self.selected=None;self.disposed=False;self.setWindowTitle(record['number']+' · 历史签发快照');self.resize(1100,730)
        v=QVBoxLayout(self);self.rev=QComboBox();self.rev.addItems([f'V{i:03d}'+(' · 当前' if i==record['revision'] else ' · 历史') for i in range(1,record['revision']+1)]);self.rev.setCurrentIndex(record['revision']-1);v.addWidget(self.rev)
        self.note=label('读取中…','muted');v.addWidget(self.note);self.search=QLineEdit();self.search.setPlaceholderText('筛选商品名称 / 编码（不影响正式导出范围）');v.addWidget(self.search)
        self.model=ArchiveModel(self);self.table=QTableView();self.table.setModel(self.model);self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers);self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows);v.addWidget(self.table,1)
        for i,width in enumerate([150,260,115,100,100,100,110,340]):self.table.setColumnWidth(i,width)
        row=QHBoxLayout();self.export_btn=button('导出此修订确认单',self.export);self.audit_btn=button('完整来源与参数审计',self.audit);row.addWidget(self.export_btn);row.addWidget(self.audit_btn);row.addWidget(button('此单操作日志',self.events));row.addWidget(button('关闭',self.reject));v.addLayout(row)
        self.debounce=QTimer(self);self.debounce.setSingleShot(True);self.debounce.setInterval(275);self.debounce.timeout.connect(self.filter);self.search.textChanged.connect(lambda _:self.debounce.start());self.search.returnPressed.connect(self.filter);self.rev.currentIndexChanged.connect(self.load);self.load()
    def load(self):
        self.selected=None;self.export_btn.setEnabled(False);self.audit_btn.setEnabled(False);self.model.replace({'rows':[],'search':[]});self.note.setText('正在读取并校验归档…（可关闭取消）')
        # Header read is tiny and binds the requested historical revision.
        try:
            repo=repository(self.host.work_repository.path)
            with repo.work.connect() as db:
                r=db.execute('SELECT o.oid,o.number,o.current,r.digest,r.saved FROM normal_orders o JOIN normal_revisions r ON o.oid=r.oid WHERE o.day=? AND o.store=? AND r.revision=?',(self.record['day'],self.record['store'],self.rev.currentIndex()+1)).fetchone()
            if r is None:raise ValueError('归档修订不存在')
            h=dict(zip(('oid','number','current_revision','digest','saved'),r));h.update(day=self.record['day'],store=self.record['store'],revision=self.rev.currentIndex()+1)
            self.host.history_reader.request({'kind':'detail','head':h},self.ready,self.failed)
        except Exception as exc:self.failed(str(exc))
    def ready(self,view):
        if self.disposed:return
        self.selected=view['head'];s=view['summary'];self.note.setText(f"确认 {self.selected['saved']} · 签发 ¥{s['amount']:,.2f} · {s['positive']} 款 · 只读；后续确认/下单/收货未接入")
        self.model.replace(view);self.filter();self.export_btn.setEnabled(True);self.audit_btn.setEnabled(True)
    def failed(self,message):
        if not self.disposed:self.note.setText('读取失败：'+message+'；重新选择修订可重试')
    def filter(self):self.debounce.stop();self.model.filter(self.search.text())
    def export(self):
        if self.selected:self.host.export_order(self.selected)
    def events(self):
        if self.selected:self.host.show_events(self.selected['oid'])
    def audit(self):
        if not self.selected:return
        dialog=AuditDialog(self,self.host,self.selected);dialog.exec();dialog.dispose();dialog.deleteLater()
    def dispose(self):
        self.disposed=True;self.debounce.stop();self.host.history_reader.cancel();self.model.replace({'rows':[],'search':[]});self.selected=None
    def done(self,code):self.dispose();super().done(code)


class AuditDialog(QDialog):
    def __init__(self,parent,host,head):
        super().__init__(parent);self.host=host;self.head=head;self.disposed=False;self.setWindowTitle('完整审计快照（内部）');self.resize(900,650);v=QVBoxLayout(self)
        v.addWidget(label('按需查看；完整快照可保存为JSON，原字段、值及类型编码保持不变。','muted'));row=QHBoxLayout();self.scope=QComboBox();self.scope.addItems(['概要','商品','参数与来源']);row.addWidget(self.scope);self.sku=QLineEdit();self.sku.setPlaceholderText('商品编码（精确匹配）');row.addWidget(self.sku);row.addWidget(button('查看',self.load));v.addLayout(row);self.text=QTextBrowser();v.addWidget(self.text,1)
        row=QHBoxLayout();row.addWidget(button('保存完整审计JSON',self.export));row.addWidget(button('关闭',self.reject));v.addLayout(row);self.load()
    def load(self):
        self.text.setPlainText('读取中…');self.host.history_reader.request({'kind':'audit','head':self.head,'field':self.scope.currentText(),'sku':self.sku.text()},self.ready,self.failed)
    def ready(self,value):
        if self.disposed:return
        text=wr.dumps(value);self.text.setPlainText(text if len(text)<=200000 else text[:200000]+'\n【仅预览前20万字符；保存完整审计JSON可获取全部内容】')
    def failed(self,message):
        if not self.disposed:self.text.setPlainText('读取失败：'+message)
    def export(self):
        path,_=QFileDialog.getSaveFileName(self,'保存完整审计快照',self.head['number']+'.json','JSON (*.json)')
        if not path:return
        self.host.history_reader.request({'kind':'audit_export','head':self.head,'path':path},lambda p:self.text.setPlainText('完整快照已保存：'+p),self.failed)
    def dispose(self):self.disposed=True;self.host.history_reader.cancel()
    def done(self,code):self.dispose();super().done(code)
