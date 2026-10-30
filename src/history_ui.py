"""Read-only archive views. Lightweight projections, no full snapshot in Qt."""
import json
from PySide6.QtCore import Qt,QAbstractTableModel,QModelIndex,QTimer
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QComboBox,QLineEdit,QTableView,QFileDialog,QTextBrowser,QMessageBox,QAbstractItemView,QProgressBar
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
        row=QHBoxLayout();self.export_btn=button('导出此修订确认单',self.export);self.audit_btn=button('当时参数 / 销量库存 / 商品资料',self.audit);row.addWidget(self.export_btn);row.addWidget(self.audit_btn);row.addWidget(button('门店确认 / 导入回传',self.feedback));row.addWidget(button('此单操作日志',self.events));row.addWidget(button('关闭',self.reject));v.addLayout(row)
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
        self.selected=view['head'];s=view['summary'];self.note.setText(f"确认 {self.selected['saved']} · 签发 ¥{s['amount']:,.2f} · {s['positive']} 款 · 原签发只读；门店确认回传独立保存")
        self.model.replace(view);self.filter();self.export_btn.setEnabled(True);self.audit_btn.setEnabled(True)
    def failed(self,message):
        if not self.disposed:self.note.setText('读取失败：'+message+'；重新选择修订可重试')
    def filter(self):self.debounce.stop();self.model.filter(self.search.text())
    def export(self):
        if self.selected:self.host.export_order(self.selected)
    def events(self):
        if self.selected:self.host.show_events(self.selected['oid'])
    def feedback(self):
        if not self.selected:return
        from feedback_ui import FeedbackDialog
        try:FeedbackDialog(self,self.host,self.selected).exec()
        except Exception as exc:QMessageBox.warning(self,'未打开回传',str(exc))
    def audit(self):
        if not self.selected:return
        dialog=AuditDialog(self,self.host,self.selected);dialog.exec();dialog.dispose();dialog.deleteLater()
    def dispose(self):
        self.disposed=True;self.debounce.stop();self.host.history_reader.cancel();self.model.replace({'rows':[],'search':[]});self.selected=None
    def done(self,code):self.dispose();super().done(code)


class AuditDialog(QDialog):
    def __init__(self,parent,host,head):
        super().__init__(parent);self.host=host;self.head=head;self.disposed=False;self.setWindowTitle('当时的补货依据 · 只读业务表单');self.resize(1150,760);v=QVBoxLayout(self)
        v.addWidget(label('所有数值取自这次签发快照；空值、0和空文本分别展示，不使用当前参数补填。','muted'))
        row=QHBoxLayout();self.scope=QComboBox();self.scope.addItems(['单据概况','当时参数','商品建议与销量库存','当时商品资料快照','商品完整计算依据']);row.addWidget(self.scope);self.sku=QLineEdit();self.sku.setPlaceholderText('商品编码（精确匹配）');row.addWidget(self.sku);row.addWidget(button('查看',self.load));v.addLayout(row)
        from business_ui import grid
        self.note=label('');v.addWidget(self.note);self.bar=QProgressBar();self.bar.setRange(0,0);v.addWidget(self.bar);self.search=QLineEdit();self.search.setPlaceholderText('在当前表单内查询字段或数值');v.addWidget(self.search);self.table,self.model=grid(self,['核对项目','当时值','类型']);self.table.setColumnWidth(0,430);self.table.setColumnWidth(1,520);v.addWidget(self.table,1);self.search.textChanged.connect(self.model.filter)
        row=QHBoxLayout();row.addWidget(button('导出当前完整表单Excel',self.export));row.addWidget(button('查看 / 下载当时原文件',self.sources));row.addWidget(button('关闭',self.reject));v.addLayout(row);self.scope.currentIndexChanged.connect(self.load);self.sku.returnPressed.connect(self.load);self.load()
    def load(self):
        self.bar.setRange(0,0);self.note.setText('正在读取当时快照…（可关闭取消）');self.model.replace([]);self.host.history_reader.request({'kind':'form','head':self.head,'scope':self.scope.currentText(),'sku':self.sku.text()},self.ready,self.failed)
    def ready(self,value):
        if self.disposed:return
        self.bar.setRange(0,1);self.bar.setValue(1);self.model.replace(value);self.model.filter(self.search.text());self.note.setText(f'已读取 {len(value)} 项；原签发建议只读')
    def failed(self,message):
        if not self.disposed:self.bar.setRange(0,1);self.bar.setValue(0);self.note.setText('读取失败：'+message)
    def export(self):
        path,_=QFileDialog.getSaveFileName(self,'导出当时业务表单',self.head['number']+'-'+self.scope.currentText()+'.xlsx','Excel (*.xlsx)')
        if path:self.host.history_reader.request({'kind':'form_export','head':self.head,'scope':self.scope.currentText(),'sku':self.sku.text(),'path':path},lambda p:self.note.setText('已保存：'+p),self.failed)
    def sources(self):
        self.host.history_reader.request({'kind':'attachments','head':self.head},self.show_sources,self.failed)
    def show_sources(self,rows):
        if self.disposed:return
        d=QDialog(self);d.setWindowTitle('当时的原文件');d.resize(1050,560);v=QVBoxLayout(d)
        from business_ui import grid
        table,model=grid(d,['资料类型','原路径','SHA256','留存状态'],rows);v.addWidget(table,1);note=label('未留存的历史原文件无法下载；不会读取同路径下今天的新文件。');v.addWidget(note)
        def save():
            i=table.currentIndex().row()
            if i<0:return
            item=model.rows[model.indices[i]]
            if item[3]!='已留存':note.setText('历史原文件未留存');return
            from pathlib import Path
            path,_=QFileDialog.getSaveFileName(d,'保存原文件副本',Path(item[1]).name,'原文件 (*)')
            if path:self.host.history_reader.request({'kind':'source_export','head':self.head,'sha':item[2],'path':path},lambda p:note.setText('已保存：'+p),note.setText)
        v.addWidget(button('下载选中的原文件',save));v.addWidget(button('关闭',d.accept));d.exec();self.host.history_reader.cancel()
    def dispose(self):self.disposed=True;self.host.history_reader.cancel()
    def done(self,code):self.dispose();super().done(code)

class EventsDialog(QDialog):
    """Virtual read-only form table; full query export in a background worker."""
    def __init__(self,parent,records,rows=None):
        super().__init__(parent);self.records=records;self.setWindowTitle('归档操作日志明细');self.resize(1180,680);v=QVBoxLayout(self);self.search=QLineEdit();self.search.setPlaceholderText('查询时间、门店、单号、动作或文件名');v.addWidget(self.search);self.note=label('');v.addWidget(self.note)
        from business_ui import grid
        from history_forms import event_rows
        self.rows=rows if rows is not None else event_rows(records,{h['oid']:h for h in parent.orders.headers()} if hasattr(parent,'orders') else {})
        self.table,self.model=grid(self,['时间','补货单号','门店','签发修订','操作','明细'],self.rows);self.table.setColumnWidth(5,540);v.addWidget(self.table,1);self.search.textChanged.connect(self.filter);row=QHBoxLayout();row.addWidget(button('导出当前查询Excel',self.export));row.addWidget(button('关闭',self.accept));v.addLayout(row);self.filter()
        self.table.doubleClicked.connect(self.open_order)
    def open_order(self,index):
        record=self.records[self.model.indices[index.row()]];host=self.parentWidget()
        if not hasattr(host,'work_repository'):return
        with host.work_repository.connect() as db:row=db.execute('SELECT day,store,number,current FROM normal_orders WHERE oid=?',(record['oid'],)).fetchone()
        if not row:return
        d=ArchiveDialog(host,dict(zip(('day','store','number','revision'),row)));d.rev.setCurrentIndex(record['revision']-1);d.exec();d.deleteLater()
    def filter(self,*_):
        self.model.filter(self.search.text());self.matches=[self.records[i] for i in self.model.indices];self.note.setText(f'匹配 {len(self.matches)} 条；双击回溯当时签发修订，导出包含全部匹配记录。')
    def export(self):
        path,_=QFileDialog.getSaveFileName(self,'导出当前查询日志','归档日志.xlsx','Excel (*.xlsx)')
        if not path:return
        from business_ui import run
        import business_excel
        try:run(self,business_excel.write,(path,[('操作日志',self.model.headers,[self.rows[i] for i in self.model.indices])]),'导出日志')
        except Exception as exc:QMessageBox.warning(self,'未导出',str(exc))
