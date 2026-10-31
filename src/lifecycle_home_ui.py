"""Home shows the earliest missing document, never an editable follow-up state."""
from datetime import date,timedelta
from PySide6.QtCore import Qt,QSignalBlocker
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QLineEdit,QComboBox,QTableWidget,QTableWidgetItem,QProgressBar,QMessageBox
from ui_controls import button,label
from history_tasks import HistoryReader
import work_repository as wr
import order_lifecycle as service
from lifecycle_ui import LifecycleDialog,import_document,import_group,export_selected,warn,review_again
from lifecycle_io import TITLES

class LifecycleWidget(QWidget):
    def __init__(self,host):
        super().__init__(host);self.host=host;self.records=[];self.page=0;self.checked={};self.reader=HistoryReader(host.work_repository.path,host.repository.folder/'lifecycle_index.sqlite3',self)
        v=QVBoxLayout(self);v.setContentsMargins(0,0,0,0);v.addWidget(label('订单进度 · 每店每仓一单','section'));filters=QHBoxLayout();self.start=QLineEdit((date.fromisoformat(wr.today())-timedelta(days=6)).isoformat());self.end=QLineEdit(wr.today())
        for e in (self.start,self.end):e.setMaximumWidth(120);e.setPlaceholderText('YYYY-MM-DD')
        filters.addWidget(label('业务日期'));filters.addWidget(self.start);filters.addWidget(label('至'));filters.addWidget(self.end);self.search=QLineEdit();self.search.setPlaceholderText('门店 / 仓 / 单号');self.search.setClearButtonEnabled(True);filters.addWidget(self.search,1);filters.addWidget(button('查询',self.query));filters.addWidget(button('全部未完成（不限日期）',self.all_pending));v.addLayout(filters)
        self.status=QComboBox();self.status.addItems(['全部记录','全部未完成',*service.STATUSES]);row=QHBoxLayout();row.addWidget(self.status);row.addWidget(label('按最早缺失单据归类；后续资料已到也会保留。','muted'),1);v.addLayout(row);self.status.currentIndexChanged.connect(self.query);self.search.returnPressed.connect(self.query)
        self.cards={};row=QHBoxLayout()
        for title in service.STATUSES:
            b=button(title+'\n—',lambda checked=False,s=title:self.status.setCurrentText(s));self.cards[title]=b;row.addWidget(b)
        v.addLayout(row);self.bar=QProgressBar();self.bar.setMaximumHeight(14);v.addWidget(self.bar);self.note=label('待查询','muted');v.addWidget(self.note)
        self.table=QTableWidget();self.table.setColumnCount(12);self.table.setHorizontalHeaderLabels(['勾选','单据详情','下一步','业务日期','门店','仓','补货单号','当前进度','门店回传','门店付款','实际收货','资料提示']);self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);self.table.itemChanged.connect(self.check);v.addWidget(self.table,1)
        row=QHBoxLayout();row.addWidget(button('勾选本页',lambda:self.select_page(True)));row.addWidget(button('清空勾选',lambda:self.select_page(False)));self.export_btn=button('导出勾选单生命周期对照',self.export);row.addWidget(self.export_btn);row.addWidget(button('上一页',lambda:self.turn(-1)));row.addWidget(button('下一页',lambda:self.turn(1)));self.selected_label=label('');row.addWidget(self.selected_label);v.addLayout(row)
        v.addWidget(import_group(self,host,refresh=self.query));v.addWidget(label('确认归档即签发。工作页可继续计算其它批次；完整单据及数量差异可在详情查看。','muted'));self.selection_label()
    def query(self,*_,keep_page=False):
        if self.host.closing:return
        if not keep_page:self.page=0;self.checked.clear()
        self.selection_label();self.table.setEnabled(False);self.export_btn.setEnabled(False);self.bar.setRange(0,0);self.note.setText('正在查询单据进度…可继续使用工作页。')
        self.reader.request({'kind':'lifecycle','from':self.start.text().strip(),'to':self.end.text().strip(),'search':self.search.text().strip(),'status':'' if self.status.currentIndex()==0 else self.status.currentText(),'page':self.page},self.ready,self.failed,self.progress)
    def progress(self,p):self.bar.setRange(0,max(1,p['total']));self.bar.setValue(p['done']);self.note.setText(f"读取归档 {p['done']} / {p['total']}")
    def ready(self,data):
        self.records=data['records'];self.page=data['page'];self.count=data['count'];self.bar.setRange(0,1);self.bar.setValue(1);self.table.setEnabled(True);self.note.setText(f"查询范围 {data['scope']} 单 · 当前状态 {data['count']} 单 · 第 {self.page+1}/{max(1,(data['count']+49)//50)} 页；每页50单，四类计数不重复。")
        for k,b in self.cards.items():b.setText(k+'\n'+str(data['counts'][k]))
        blocker=QSignalBlocker(self.table);self.table.setRowCount(len(self.records))
        for i,h in enumerate(self.records):
            c=QTableWidgetItem();c.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsUserCheckable);c.setCheckState(Qt.CheckState.Checked if service.key(h) in self.checked else Qt.CheckState.Unchecked);self.table.setItem(i,0,c)
            notices=[h['note']] if h.get('note') else []
            if h['legacy_incomplete']:notices.append('已有旧回传资料，请导入完整确认单')
            if h['old_revision']:notices.append('旧签发修订另有下游资料，未转移至本修订')
            if h['warehouse']=='旧版未标仓':notices.append('仓归属待核，不能自动关联')
            values=[h['day'],h['name']+'（'+h['store']+'）',h['warehouse'],h['number'],h['status'],*[('已导入' if h['present'][s] else '未导入') for s in service.STAGES],'；'.join(notices)]
            for j,x in enumerate(values,3):self.table.setItem(i,j,QTableWidgetItem(str(x)))
            self.table.setCellWidget(i,1,button('查看生命周期',lambda checked=False,r=h:self.open_comparison(r)))
            recheck=h['next'] and h['present'][h['next']]
            b=button(('重新核对' if recheck else '导入')+TITLES[h['next']] if h['next'] else '无后续待办',lambda checked=False,r=h:self.next(r));b.setEnabled(h['next'] is not None and h['warehouse']!='旧版未标仓');self.table.setCellWidget(i,2,b)
        del blocker
        for i,width in enumerate([45,110,165,100,185,70,250,85,90,90,90,260]):self.table.setColumnWidth(i,width)
        self.selection_label()
    def failed(self,message):self.records=[];self.table.setRowCount(0);self.bar.setRange(0,1);self.bar.setValue(0);self.note.setText('查询失败：'+message+'；可重新查询');self.table.setEnabled(True)
    def check(self,item):
        if item.column()!=0 or item.row()>=len(self.records):return
        h=self.records[item.row()]
        if item.checkState()==Qt.CheckState.Checked:self.checked[service.key(h)]=h
        else:self.checked.pop(service.key(h),None)
        self.selection_label()
    def selection_label(self):self.selected_label.setText(f'已勾选 {len(self.checked)} 单');self.export_btn.setEnabled(bool(self.checked))
    def select_page(self,on):
        if not on:self.checked.clear()
        for i in range(self.table.rowCount()):self.table.item(i,0).setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        self.selection_label()
    def turn(self,n):self.page=max(0,self.page+n);self.query(keep_page=True)
    def all_pending(self):self.start.clear();self.end.clear();b=QSignalBlocker(self.status);self.status.setCurrentText('全部未完成');del b;self.query()
    def export(self):export_selected(self,self.host,sorted(self.checked.values(),key=lambda h:h['list_position']))
    def open_comparison(self,head):
        try:self.host.open_archive(head,'lifecycle');self.query(keep_page=True)
        except Exception as exc:warn(self,exc)
    def next(self,head):
        s=head['next']
        if not s:return
        result=review_again(self,self.host,head,s) if head['present'][s] else import_document(self,self.host,s,head)
        if result:self.query(keep_page=True)
    def shutdown(self):self.reader.shutdown()
