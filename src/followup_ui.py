"""Home todo view: bounded pages, background queries, explicit selected scopes."""
from datetime import date,timedelta
from PySide6.QtCore import Qt,QTimer,QSignalBlocker
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QLineEdit,QComboBox,QTableWidget,QTableWidgetItem,QProgressBar,QMessageBox,QInputDialog
from ui_controls import button,label
from history_tasks import HistoryReader
from business_ui import run
from interaction_ui import confirm
import followup,work_repository as wr
from purchase_ui import ComparisonDialog,PurchaseLibrary,import_purchases,download_template,export_selected,warn

class FollowupWidget(QWidget):
    def __init__(self,host):
        super().__init__(host);self.host=host;self.records=[];self.page=0;self.checked={};self.reader=HistoryReader(host.work_repository.path,host.repository.folder/'followup_index.sqlite3',self)
        v=QVBoxLayout(self);v.setContentsMargins(0,0,0,0);v.addWidget(label('补货跟进 · 每店每仓一单','section'));filters=QHBoxLayout();self.start=QLineEdit((date.fromisoformat(wr.today())-timedelta(days=6)).isoformat());self.end=QLineEdit(wr.today())
        for e in (self.start,self.end):e.setMaximumWidth(120);e.setPlaceholderText('YYYY-MM-DD')
        filters.addWidget(label('业务日期'));filters.addWidget(self.start);filters.addWidget(label('至'));filters.addWidget(self.end);self.search=QLineEdit();self.search.setPlaceholderText('门店 / 仓 / 单号');self.search.setClearButtonEnabled(True);filters.addWidget(self.search,1);filters.addWidget(button('查询',self.query));filters.addWidget(button('全部未完成（不限日期）',self.all_pending));v.addLayout(filters)
        self.status=QComboBox();self.status.addItems(['全部记录','全部未完成','待回传','部分回传','尚未关联下单资料','部分关联','差异待核','旧修订待核','暂缓跟进']);r=QHBoxLayout();r.addWidget(self.status);self.counts=label('','muted');r.addWidget(self.counts,1);v.addLayout(r);self.status.currentIndexChanged.connect(self.query);self.search.returnPressed.connect(self.query)
        self.cards={};cards=QHBoxLayout()
        for title in ('待回传','部分回传','尚未关联下单资料','部分关联','差异待核','旧修订待核'):
            b=button(title+'\n—',lambda checked=False,s=title:self.status.setCurrentText(s));self.cards[title]=b;cards.addWidget(b)
        v.addLayout(cards)
        self.bar=QProgressBar();self.bar.setMaximumHeight(14);v.addWidget(self.bar);self.note=label('待查询','muted');v.addWidget(self.note)
        self.table=QTableWidget();self.table.setColumnCount(12);self.table.setHorizontalHeaderLabels(['勾选','签发依据','执行对照','业务日期','门店','仓','签发单 / 修订','回传状态','下单状态','需处理','已等待天数','跟进记录']);self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);self.table.itemChanged.connect(self.check);v.addWidget(self.table,1)
        r=QHBoxLayout();r.addWidget(button('勾选本页',lambda:self.select_page(True)));r.addWidget(button('清空勾选',lambda:self.select_page(False)));self.export_btn=button('导出勾选单的执行对照',self.export);r.addWidget(self.export_btn);r.addWidget(button('上一页',lambda:self.turn(-1)));r.addWidget(button('下一页',lambda:self.turn(1)));self.selected_label=label('');r.addWidget(self.selected_label);v.addLayout(r)
        r=QHBoxLayout();r.addWidget(button('导入实际下单资料',self.import_files,True));r.addWidget(button('采购资料与版本',self.library));r.addWidget(button('下载下单模板',lambda:download_template(self)));r.addWidget(button('暂缓选中一单',lambda:self.set_note('暂缓跟进')));r.addWidget(button('恢复选中一单跟进',lambda:self.set_note('恢复跟进')));r.addWidget(button('已核实选中单差异',lambda:self.set_note('已核实保留差异')));v.addLayout(r)
        v.addWidget(label('勾选用于批量导出；暂缓/恢复/核实仅作用于当前高亮一单。未知下单量不当作0；收货关联后续接入。','muted'));self.selection_label()
    def query(self,*_,keep_page=False):
        if self.host.closing:return
        if not keep_page:self.page=0;self.checked.clear()
        self.selection_label();self.table.setEnabled(False);self.export_btn.setEnabled(False);self.bar.setRange(0,0);self.note.setText('正在查询；未完成的查询不代表没有待办。可继续使用工作页。')
        request={'kind':'followup','from':self.start.text().strip(),'to':self.end.text().strip(),'search':self.search.text().strip(),'status':'' if self.status.currentIndex()==0 else self.status.currentText(),'page':self.page}
        self.reader.request(request,self.ready,self.failed,self.progress)
    def progress(self,p):self.bar.setRange(0,max(1,p['total']));self.bar.setValue(p['done']);self.note.setText(f"核对归档 {p['done']} / {p['total']}")
    def ready(self,data):
        self.records=data['records'];self.page=data['page'];self.count=data['count'];self.bar.setRange(0,1);self.bar.setValue(1);self.table.setEnabled(True);self.counts.setText('下方数字为当前日期/关键词范围内的订单数，可点选；各类可能重叠，暂缓单不计入待办。');self.note.setText(f"日期/关键词范围内 {data['scope']} 单 · 当前状态 {data['count']} 单 · 第 {self.page+1} 页 / {max(1,(data['count']+49)//50)} 页；每页50单。")
        for k,b in self.cards.items():b.setText(k+'\n'+str(data['counts'][k]))
        blocker=QSignalBlocker(self.table);self.table.setRowCount(len(self.records))
        for i,h in enumerate(self.records):
            c=QTableWidgetItem();c.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsUserCheckable);c.setCheckState(Qt.CheckState.Checked if (h['oid'],h['revision'],h['warehouse']) in self.checked else Qt.CheckState.Unchecked);self.table.setItem(i,0,c)
            note=h['note'];values=[h['day'],h['name']+' ('+h['store']+')',h['warehouse'],h['number']+f" / V{h['revision']:03d}",h['reply_status'],h['purchase_status'],'暂缓跟进' if h['deferred'] else ' / '.join(h['issues']) or '当前无待办',h['wait_days'],(note.get('action','')+'：'+note.get('reason','')+('（资料已更新，需重新核实）' if note.get('action')=='已核实保留差异' and not h['acknowledged'] else '')) if note else '']
            for j,x in enumerate(values,3):self.table.setItem(i,j,QTableWidgetItem(str(x)))
            self.table.setCellWidget(i,1,button('查看签发',lambda checked=False,r=h:self.host.open_archive(r)));self.table.setCellWidget(i,2,button('查看执行对照',lambda checked=False,r=h:self.open_comparison(r)))
        del blocker
        for i,w in enumerate([50,90,115,100,190,80,220,95,150,180,90,180]):self.table.setColumnWidth(i,w)
        self.selection_label()
    def failed(self,message):self.records=[];self.table.setRowCount(0);self.bar.setRange(0,1);self.bar.setValue(0);self.note.setText('查询失败：'+message+'；可重新查询');self.table.setEnabled(True)
    def check(self,item):
        if item.column()!=0 or item.row()>=len(self.records):return
        h=self.records[item.row()];k=(h['oid'],h['revision'],h['warehouse'])
        if item.checkState()==Qt.CheckState.Checked:self.checked[k]=h
        else:self.checked.pop(k,None)
        self.selection_label()
    def selection_label(self):self.selected_label.setText(f'已勾选 {len(self.checked)} 单');self.export_btn.setEnabled(bool(self.checked))
    def select_page(self,on):
        if not on:self.checked.clear()
        for i in range(self.table.rowCount()):self.table.item(i,0).setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        self.selection_label()
    def turn(self,n):self.page=max(0,self.page+n);self.query(keep_page=True)
    def all_pending(self):
        self.start.clear();self.end.clear();b=QSignalBlocker(self.status);self.status.setCurrentText('全部未完成');del b;self.query()
    def export(self):
        # Page order, never checkbox click order.
        export_selected(self,self.host,sorted(self.checked.values(),key=lambda h:h['list_position']))
    def import_files(self):
        if import_purchases(self,self.host):self.query(keep_page=True)
    def library(self):PurchaseLibrary(self,self.host).exec();self.query(keep_page=True)
    def open_comparison(self,head):ComparisonDialog(self,self.host,head).exec();self.query(keep_page=True)
    def set_note(self,action):
        n=self.table.currentRow()
        if n<0 or n>=len(self.records):QMessageBox.warning(self,'尚未选中','请先高亮需要处理的一张仓单。');return
        head=self.records[n]
        if action=='恢复跟进' and not head['deferred']:QMessageBox.information(self,'无需恢复','此单当前没有生效中的暂缓记录。');return
        if action=='已核实保留差异' and not (head['differences'] or head['old_revision']):QMessageBox.information(self,'无需核实','此单当前没有已发现的数量差异或旧修订关联。');return
        reason,ok=QInputDialog.getText(self,action,head['number']+f" / V{head['revision']:03d}\n请填写原因；仅记录跟进，不调整任何数量。")
        if not ok:return
        until=''
        if action=='暂缓跟进':
            until,ok=QInputDialog.getText(self,'暂缓截止','填写恢复跟进日期 YYYY-MM-DD；留空表示手动恢复。')
            if not ok:return
        if not confirm(self,action,head['number']+'\n'+reason+'\n确认仅记录这张仓单的跟进状态？'):return
        try:run(self,followup.note,(self.host.work_repository.path,head,action,reason,until),'保存跟进记录',cancellable=False);self.query(keep_page=True)
        except Exception as exc:warn(self,exc)
    def shutdown(self):self.reader.shutdown()
