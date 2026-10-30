"""Read-only archive views. Lightweight projections, no full snapshot in Qt."""
import json
from contextlib import closing
from PySide6.QtGui import QShortcut,QKeySequence
from PySide6.QtCore import Qt,QAbstractTableModel,QModelIndex,QTimer
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QComboBox,QLineEdit,QTableView,QFileDialog,QTextBrowser,QMessageBox,QAbstractItemView,QProgressBar,QSplitter,QWidget
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
        from history_tasks import HistoryReader
        self.reader=HistoryReader(host.work_repository.path,host.history_reader.cache,self)
        v=QVBoxLayout(self);self.rev=QComboBox();self.rev.addItems([f'V{i:03d}'+(' · 当前' if i==record['revision'] else ' · 历史') for i in range(1,record['revision']+1)]);self.rev.setCurrentIndex(record['revision']-1);v.addWidget(self.rev)
        self.note=label('读取中…','muted');v.addWidget(self.note);self.search=QLineEdit();self.search.setPlaceholderText('筛选商品名称 / 编码（不影响正式导出范围）');v.addWidget(self.search)
        self.model=ArchiveModel(self);self.table=QTableView();self.table.setModel(self.model);self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers);self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows);v.addWidget(self.table,1)
        for i,width in enumerate([150,260,115,100,100,100,110,340]):self.table.setColumnWidth(i,width)
        self.table.doubleClicked.connect(lambda _:self.audit())
        exports=QHBoxLayout();self.archive_export_buttons=[]
        for title,kind in [('导出完整计算表','calculation'),('下载商品资料原表','original'),('导出签发建议','signed')]:
            b=button(title,lambda checked=False,k=kind:self.export_archive(k));exports.addWidget(b);self.archive_export_buttons.append(b)
        self.archive_export_btn=self.archive_export_buttons[0];v.insertLayout(1,exports)
        row=QHBoxLayout();self.export_btn=button('导出此修订确认单',self.export);self.audit_btn=button('查看计算依据',self.audit);row.addWidget(self.export_btn);row.addWidget(self.audit_btn);row.addWidget(button('查看门店回传',self.feedback));row.addWidget(button('此单操作日志',self.events));row.addWidget(button('关闭',self.reject));v.addLayout(row)
        row.insertWidget(3,button('查看实际下单对照',self.purchases))
        self.debounce=QTimer(self);self.debounce.setSingleShot(True);self.debounce.setInterval(275);self.debounce.timeout.connect(self.filter);self.search.textChanged.connect(lambda _:self.debounce.start());self.search.returnPressed.connect(self.filter);self.rev.currentIndexChanged.connect(self.load);self.load()
    def load(self):
        if self.disposed:return
        self.selected=None;self.archive_export_btn.setEnabled(False);self.export_btn.setEnabled(False);self.audit_btn.setEnabled(False);self.model.replace({'rows':[],'search':[]});self.note.setText('正在读取并校验归档…（可关闭取消）')
        for b in self.archive_export_buttons:b.setEnabled(False)
        # Header read is tiny and binds the requested historical revision.
        try:
            repo=repository(self.host.work_repository.path)
            with closing(repo.work.connect()) as db:
                r=db.execute('SELECT o.oid,o.number,o.current,r.digest,r.saved FROM normal_orders o JOIN normal_revisions r ON o.oid=r.oid WHERE o.day=? AND o.store=? AND r.revision=?',(self.record['day'],self.record['store'],self.rev.currentIndex()+1)).fetchone()
            if r is None:raise ValueError('归档修订不存在')
            h=dict(zip(('oid','number','current_revision','digest','saved'),r));h.update(day=self.record['day'],store=self.record['store'],revision=self.rev.currentIndex()+1,warehouse=self.record.get('warehouse'))
            self.reader.request({'kind':'detail','head':h},self.ready,self.failed)
        except Exception as exc:self.failed(str(exc))
    def ready(self,view):
        if self.disposed:return
        self.selected=view['head'];s=view['summary'];self.note.setText(f"确认 {self.selected['saved']} · 签发 ¥{s['amount']:,.2f} · {s['positive']} 款 · {self.selected.get('warehouse','')} · {'回收站（可查看，恢复后导出）' if self.selected.get('deleted') else '原签发只读；回传独立保存'}")
        self.selected['name']=self.record.get('name',self.record['store'])
        self.model.replace(view);self.filter();self.export_btn.setEnabled(not self.selected.get('deleted'));self.archive_export_btn.setEnabled(not self.selected.get('deleted'));self.audit_btn.setEnabled(True)
        for b in self.archive_export_buttons:b.setEnabled(not self.selected.get('deleted'))
        if self.model.rowCount():self.table.selectRow(0)
        self.audit_btn.setText('查看计算依据')
        self.export_btn.setToolTip('导出此门店、此修订的完整中心仓确认单；不受商品搜索过滤影响。')
    def failed(self,message):
        if not self.disposed:self.note.setText('读取失败：'+message+'；重新选择修订可重试')
    def filter(self):self.debounce.stop();self.model.filter(self.search.text())
    def export(self):
        if self.selected:self.host.export_order(self.selected)
    def export_archive(self,kind='calculation'):
        if self.selected:self.host.export_archive(self.selected,kind)
    def events(self):
        if self.selected:self.host.show_events(self.selected['oid'],self.selected.get('warehouse'))
    def feedback(self):
        if not self.selected:return
        from feedback_ui import FeedbackDialog
        try:FeedbackDialog(self,self.host,self.selected).exec()
        except Exception as exc:QMessageBox.warning(self,'未打开回传',str(exc))
    def purchases(self):
        if not self.selected:return
        from purchase_ui import ComparisonDialog
        try:ComparisonDialog(self,self.host,self.selected).exec()
        except Exception as exc:QMessageBox.warning(self,'未打开执行对照',str(exc))
    def audit(self):
        if not self.selected:return
        i=self.table.currentIndex().row();sku=self.model.rows[self.model.indices[i]][0] if i>=0 else ''
        dialog=AuditDialog(self,self.host,self.selected,products=self.model.rows,sku=sku);dialog.exec();dialog.dispose();dialog.deleteLater()
    def dispose(self):
        self.disposed=True;self.debounce.stop();self.reader.shutdown();self.model.replace({'rows':[],'search':[]});self.selected=None
    def done(self,code):self.dispose();super().done(code)


class AuditDialog(QDialog):
    def __init__(self,parent,host,head,products=None,sku=''):
        super().__init__(parent);self.host=host;self.head=head;self.disposed=False;self.loaded_key=None;self.setWindowTitle('当时的补货依据 · 只读业务表单');self.resize(1250,790);v=QVBoxLayout(self)
        from history_tasks import HistoryReader
        from business_ui import grid
        self.reader=HistoryReader(host.work_repository.path,host.history_reader.cache,self)
        v.addWidget(label(head['number']+f" · V{head['revision']:03d} · {head['day']}",'section'))
        v.addWidget(label('左侧搜商品并点选，右侧切换当时参数、销量库存或商品资料。所有数据来自本次签发，不使用今天的资料补填。','muted'))
        splitter=QSplitter();left=QWidget();lv=QVBoxLayout(left);self.product_search=QLineEdit();self.product_search.setPlaceholderText('搜索本单商品名称 / 编码，空格分词');self.product_search.setClearButtonEnabled(True);lv.addWidget(self.product_search)
        self.products,self.product_model=grid(left,['商品编码','商品名称']);self.products.setColumnWidth(0,170);self.products.setColumnWidth(1,270);lv.addWidget(self.products,1);self.product_count=label('');lv.addWidget(self.product_count);splitter.addWidget(left)
        right=QWidget();rv=QVBoxLayout(right);row=QHBoxLayout();self.scope=QComboBox();self.scope.addItems(['单据概况','当时参数','商品建议与销量库存','当时商品资料快照','商品完整计算依据']);self.scope.setCurrentIndex(2 if sku else 1);row.addWidget(self.scope);self.sku=QLineEdit(sku);self.sku.setPlaceholderText('也可输入精确商品编码后回车');row.addWidget(self.sku);self.load_btn=button('重新读取当前依据',self.load);row.addWidget(self.load_btn);rv.addLayout(row)
        self.note=label('');rv.addWidget(self.note);self.bar=QProgressBar();self.bar.setRange(0,0);rv.addWidget(self.bar);self.search=QLineEdit();self.search.setPlaceholderText('筛选本表单字段 / 数值；导出仍包含完整表单');self.search.setClearButtonEnabled(True);rv.addWidget(self.search);self.table,self.model=grid(right,['核对项目','当时值','类型']);self.table.setColumnWidth(0,330);self.table.setColumnWidth(1,430);self.table.setColumnWidth(2,65);rv.addWidget(self.table,1);self.search.textChanged.connect(self.filter_fields)
        self.export_hint=label('','muted');rv.addWidget(self.export_hint);row=QHBoxLayout();self.export_btn=button('导出此核对表单（非原表）',self.export);self.export_btn.setEnabled(False);row.addWidget(self.export_btn);self.original_btn=button('下载商品资料原表',lambda:self.host.export_archive(self.head,'original'));self.original_btn.setEnabled(not head.get('deleted'));row.addWidget(self.original_btn);row.addWidget(button('查看原始附件',self.sources));row.addWidget(button('关闭',self.reject));rv.addLayout(row);splitter.addWidget(right);splitter.setSizes([360,860]);v.addWidget(splitter,1)
        self.product_search.textChanged.connect(self.filter_products);self.products.clicked.connect(self.select_product);self.scope.currentIndexChanged.connect(self.load);self.sku.returnPressed.connect(self.load)
        self.search_shortcut=QShortcut(QKeySequence('Ctrl+F'),self);self.search_shortcut.activated.connect(self.product_search.setFocus)
        if products is None:self.reader.request({'kind':'detail','head':head},lambda view:self.set_products(view['rows'],sku),self.failed)
        else:self.set_products(products,sku)
    def set_products(self,rows,sku):
        if self.disposed:return
        self.product_model.replace([[r[0],r[1]] for r in rows]);self.filter_products()
        index=next((i for i,r in enumerate(self.product_model.rows) if r[0]==sku),0)
        if rows:self.products.selectRow(index);self.sku.setText(self.product_model.rows[index][0])
        self.load()
    def filter_products(self,*_):
        self.product_model.filter(self.product_search.text());self.product_count.setText(f'找到 {self.product_model.rowCount()} / {len(self.product_model.rows)} 个商品；点击一行查看')
    def select_product(self,index):
        self.sku.setText(self.product_model.rows[self.product_model.indices[index.row()]][0])
        if not self.scope.currentText().startswith('商品') and self.scope.currentText()!='当时商品资料快照':self.scope.setCurrentText('商品建议与销量库存')
        else:self.load()
    def filter_fields(self,*_):
        self.model.filter(self.search.text())
        if self.loaded_key:self.note.setText(f'已读取 {len(self.model.rows)} 项 · 当前显示 {self.model.rowCount()} 项；签发 V{self.head["revision"]:03d} 只读')
    def load(self):
        if self.disposed:return
        self.load_btn.setEnabled(False);self.load_btn.setText('正在读取依据…');self.loaded_key=None;self.export_btn.setEnabled(False);self.bar.setRange(0,0);self.note.setText('正在读取并校验当时快照…（可关闭取消，不必重复点击）');self.model.replace([])
        scope=self.scope.currentText();sku=self.sku.text().strip();self.export_hint.setText(f'导出范围：本单 V{self.head["revision"]:03d} · {scope}'+(f' · 商品 {sku}' if scope not in ('单据概况','当时参数') else '')+' · 完整字段；不是加盟商确认单。')
        if scope not in ('单据概况','当时参数') and not any(r[0]==sku for r in self.product_model.rows):self.failed('请从左侧选择本单商品，或输入本单精确商品编码');return
        self.reader.request({'kind':'form','head':self.head,'scope':scope,'sku':sku},lambda value:self.ready(value,(scope,sku)),self.failed)
    def ready(self,value,key=None):
        if self.disposed:return
        self.loaded_key=key or (self.scope.currentText(),self.sku.text().strip());self.bar.setRange(0,1);self.bar.setValue(1);self.model.replace(value);self.filter_fields();self.export_btn.setEnabled(bool(value))
        original=self.scope.currentText()=='当时商品资料快照'
        self.export_btn.setEnabled(bool(value) and not original and not self.head.get('deleted'));self.export_btn.setToolTip('商品资料说明不能作为原表导出；请使用独立原表下载按钮' if original else '导出当前范围的完整字段，不受字段搜索过滤影响');self.load_btn.setEnabled(True);self.load_btn.setText('重新读取当前依据')
        if original:self.export_hint.setText('当前显示的是资料核对说明；下载将读取当时留存的原Excel，未留存时明确提示。')
    def failed(self,message):
        if not self.disposed:self.load_btn.setEnabled(True);self.load_btn.setText('重试加载依据');self.loaded_key=None;self.export_btn.setEnabled(False);self.bar.setRange(0,1);self.bar.setValue(0);self.note.setText('未能显示：'+message+'。可点击查看 / 重试。')
    def export(self):
        if self.scope.currentText()=='当时商品资料快照':return
        key=(self.scope.currentText(),self.sku.text().strip())
        if self.loaded_key!=key:self.load();return
        scope,sku=key;name=self.head['number']+f'-V{self.head["revision"]:03d}-'+scope+('' if scope in ('单据概况','当时参数') else '-'+sku)+'.xlsx'
        path,_=QFileDialog.getSaveFileName(self,'导出已显示的完整表单（不是门店确认单）',name,'Excel (*.xlsx)')
        if path:
            self.export_btn.setEnabled(False);self.bar.setRange(0,0);self.note.setText('正在导出所选修订完整表单…')
            self.reader.request({'kind':'form_export','head':self.head,'scope':scope,'sku':sku,'path':path},self.exported,self.failed)
    def exported(self,path):
        if not self.disposed:self.bar.setRange(0,1);self.bar.setValue(1);self.export_btn.setEnabled(True);self.note.setText('已保存完整表单：'+path)
    def sources(self):
        self.reader.request({'kind':'attachments','head':self.head},self.show_sources,self.failed)
    def show_sources(self,rows):
        if self.disposed:return
        d=QDialog(self);d.setWindowTitle('当时的原文件');d.resize(1050,560);v=QVBoxLayout(d)
        from business_ui import grid
        table,model=grid(d,['资料类型','原路径','SHA256','留存状态'],rows);v.addWidget(table,1);note=label('未留存的历史原文件无法下载；不会读取同路径下今天的新文件。');v.addWidget(note)
        def save():
            if self.head.get('deleted'):note.setText('此仓单在回收站，请先恢复再下载');return
            i=table.currentIndex().row()
            if i<0:return
            item=model.rows[model.indices[i]]
            if item[3]!='已留存':note.setText('历史原文件未留存');return
            from pathlib import Path
            path,_=QFileDialog.getSaveFileName(d,'保存原文件副本',Path(item[1]).name,'原文件 (*)')
            if path:self.reader.request({'kind':'source_export','head':self.head,'sha':item[2],'path':path},lambda p:note.setText('已保存：'+p),note.setText)
        v.addWidget(button('下载选中的原文件',save));v.addWidget(button('关闭',d.accept));d.exec();self.reader.cancel();self.load()
    def dispose(self):self.disposed=True;self.reader.shutdown()
    def done(self,code):self.dispose();super().done(code)

class EventsDialog(QDialog):
    """Virtual read-only form table; full query export in a background worker."""
    def __init__(self,parent,records,rows=None):
        super().__init__(parent);self.records=records;self.setWindowTitle('归档操作日志明细');self.resize(1180,680);v=QVBoxLayout(self);self.search=QLineEdit();self.search.setPlaceholderText('查询时间、门店、单号、动作或文件名');v.addWidget(self.search);self.note=label('');v.addWidget(self.note)
        from business_ui import grid
        from history_forms import event_rows
        self.rows=rows if rows is not None else event_rows(records,{h['oid']:h for h in parent.orders.headers()} if hasattr(parent,'orders') else {})
        filters=QHBoxLayout();self.day=QComboBox();self.day.addItem('全部日期','');self.action=QComboBox();self.action.addItem('全部操作','')
        for day in dict.fromkeys(r[0][:10] for r in self.rows):self.day.addItem(day,day)
        for action in dict.fromkeys(r[4] for r in self.rows):self.action.addItem(action,action)
        filters.addWidget(self.day);filters.addWidget(self.action);filters.addWidget(label('本页查询操作过程；商品资料和清单请打开对应签发单。','muted'),1);v.addLayout(filters)
        self.table,self.model=grid(self,['时间','补货单号','门店','签发修订','操作','明细'],self.rows);self.table.setColumnWidth(5,540);v.addWidget(self.table,1);self.search.textChanged.connect(self.filter);row=QHBoxLayout()
        row.addWidget(label('当前记录关联单据：先查看确切修订，再使用独立导出入口。','muted'));self.open_btn=button('查看当前记录关联单据',lambda:self.open_order(self.table.currentIndex()));row.addWidget(self.open_btn)
        row.addWidget(button('关闭',self.accept));v.addLayout(row);self.filter();self.table.selectionModel().currentRowChanged.connect(self.selection_changed)
        self.table.doubleClicked.connect(self.open_order)
        self.day.currentIndexChanged.connect(self.filter);self.action.currentIndexChanged.connect(self.filter);self.search.setClearButtonEnabled(True)
        self.search_shortcut=QShortcut(QKeySequence('Ctrl+F'),self);self.search_shortcut.activated.connect(self.search.setFocus)
    def selection_changed(self,*_):
        i=self.table.currentIndex().row();valid=0<=i<len(self.model.indices);self.open_btn.setEnabled(valid)
        if valid:
            r=self.records[self.model.indices[i]];self.open_btn.setToolTip(str(r.get('child_number') or r.get('oid'))+f" · V{r['revision']:03d}")
    def open_order(self,index):
        if not index.isValid():return
        record=self.records[self.model.indices[index.row()]];host=self.parentWidget()
        if not hasattr(host,'work_repository'):return
        with closing(host.work_repository.connect()) as db:row=db.execute('SELECT day,store,number,current FROM normal_orders WHERE oid=?',(record['oid'],)).fetchone()
        if not row:return
        head=dict(zip(('day','store','number','revision'),row));head['warehouse']=record.get('warehouse');head['number']=record.get('child_number',head['number']);d=ArchiveDialog(host,head);d.rev.setCurrentIndex(record['revision']-1);d.exec();d.deleteLater()
    def filter(self,*_):
        self.model.filter(self.search.text());self.model.beginResetModel();self.model.indices=[i for i in self.model.indices if (not self.day.currentData() or self.rows[i][0].startswith(self.day.currentData())) and (not self.action.currentData() or self.rows[i][4]==self.action.currentData())];self.model.endResetModel();self.matches=[self.records[i] for i in self.model.indices];self.note.setText(f'匹配 {len(self.matches)} / {len(self.records)} 条；双击回溯当时签发修订，打开当前记录关联单据后，可导出其确切仓单修订。');self.selection_changed()
    def export(self,kind='calculation'):
        i=self.table.currentIndex().row()
        if i<0:
            QMessageBox.information(self,'请选择记录','点选一条操作记录，导出其对应仓与签发修订的数据。');return
        record=self.records[self.model.indices[i]];host=self.parentWidget()
        if not record.get('warehouse'):return
        with closing(host.work_repository.connect()) as db:
            row=db.execute('SELECT o.day,o.store,o.number,o.current,r.digest,r.saved FROM normal_orders o JOIN normal_revisions r ON o.oid=r.oid WHERE o.oid=? AND r.revision=?',(record['oid'],record['revision'])).fetchone()
            from archive_orders import states,number
            state=states(db).get((record['oid'],record['warehouse']),{'deleted':False,'token':0})
        if not row:return
        head=dict(zip(('day','store','parent_number','current_revision','digest','saved'),row))
        head.update(oid=record['oid'],revision=record['revision'],warehouse=record['warehouse'],number=number(head['parent_number'],record['warehouse']),deleted=state['deleted'],visibility_token=state['token'])
        host.export_archive(head,kind)
