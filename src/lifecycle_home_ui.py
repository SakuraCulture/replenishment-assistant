"""Home shows the earliest missing document, never an editable follow-up state."""
from datetime import date,timedelta
from PySide6.QtCore import Qt,QSignalBlocker
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QLineEdit,QComboBox,QTableWidget,QTableWidgetItem,QProgressBar,QMessageBox,QTabWidget
from ui_controls import button,label
from history_tasks import HistoryReader
import work_repository as wr
import order_lifecycle as service
import daily_batch
from lifecycle_ui import LifecycleDialog,import_document,import_group,export_selected,warn,review_again
from lifecycle_io import TITLES

class LifecycleWidget(QWidget):
    def __init__(self,host):
        super().__init__(host);self.host=host;self.records=[];self.page=0;self.checked={};self.reader=HistoryReader(host.work_repository.path,host.repository.folder/'lifecycle_index.sqlite3',self)
        v=QVBoxLayout(self);v.setContentsMargins(0,0,0,0);v.addWidget(label('日批次待办与订单 · 每店每仓一单','section'));filters=QHBoxLayout();self.start=QLineEdit(wr.today());self.end=QLineEdit(wr.today())
        for e in (self.start,self.end):e.setMaximumWidth(120);e.setPlaceholderText('YYYY-MM-DD')
        filters.addWidget(label('业务日期'));filters.addWidget(self.start);filters.addWidget(label('至'));filters.addWidget(self.end);self.search=QLineEdit();self.search.setPlaceholderText('门店 / 仓 / 单号');self.search.setClearButtonEnabled(True);filters.addWidget(self.search,1);filters.addWidget(button('查询',self.query));filters.addWidget(button('全部未完成（不限日期）',self.all_pending));v.addLayout(filters)
        self.status=QComboBox();self.status.addItems(['全部记录','全部未完成',*daily_batch.STATUSES]);row=QHBoxLayout();row.addWidget(self.status);self.warehouse_filter=QComboBox();self.warehouse_filter.addItems(['全部仓','河北仓','山东仓']);row.addWidget(self.warehouse_filter);self.lock_filter=QComboBox();self.lock_filter.addItems(['全部锁状态','已锁','未锁']);row.addWidget(self.lock_filter);row.addWidget(label('一天一批，各单独立推进；锁单不等于付款。','muted'),1);v.addLayout(row);self.status.currentIndexChanged.connect(self.query);self.warehouse_filter.currentIndexChanged.connect(self.query);self.lock_filter.currentIndexChanged.connect(self.query);self.search.returnPressed.connect(self.query)
        self.cards={};row=QHBoxLayout()
        for title in daily_batch.STATUSES:
            b=button(title+'\n—',lambda checked=False,s=title:self.status.setCurrentText(s));self.cards[title]=b;row.addWidget(b)
        v.addLayout(row);self.bar=QProgressBar();self.bar.setMaximumHeight(14);v.addWidget(self.bar);self.note=label('待查询','muted');v.addWidget(self.note)
        self.table=QTableWidget();self.table.setColumnCount(12);self.table.setHorizontalHeaderLabels(['勾选','单据详情','下一步','业务日期','门店','仓','补货单号','当前进度','门店回传','门店付款','实际收货','资料提示']);self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);self.table.itemChanged.connect(self.check);v.addWidget(self.table,1)
        row=QHBoxLayout();row.addWidget(button('勾选本页',lambda:self.select_page(True)));row.addWidget(button('清空勾选',lambda:self.select_page(False)));self.export_btn=button('导出勾选单生命周期对照',self.export);row.addWidget(self.export_btn);row.addWidget(button('上一页',lambda:self.turn(-1)));row.addWidget(button('下一页',lambda:self.turn(1)));self.selected_label=label('');row.addWidget(self.selected_label);v.addLayout(row)
        self.actions=QTabWidget();groups={}
        for title in ('流程处理','业务文件','依据复查','维护'):
            page=QWidget();groups[title]=QVBoxLayout(page);self.actions.addTab(page,title)
        v.addWidget(self.actions)
        exports=QHBoxLayout();exports.addWidget(button('导出签发单',self.export_signed))
        for s in service.STAGES:exports.addWidget(button('导出'+TITLES[s],lambda checked=False,k=s:export_selected(self,self.host,list(self.checked.values()),k)))
        groups['业务文件'].addLayout(exports)
        from jst_ui import generate,export_orders,change_lock,Logs,cancel_orders,detail
        actions=QHBoxLayout();actions.addWidget(label('聚水潭执行'))
        actions.addWidget(button('生成勾选订单',lambda:self.jst_action(generate)))
        groups['业务文件'].addWidget(button('导出勾选聚水潭订单',lambda:self.jst_action(export_orders)))
        actions.addWidget(button('确认已下单并锁定',lambda:self.jst_action(change_lock)))
        groups['流程处理'].addLayout(actions)
        groups['依据复查'].addWidget(button('批次作业日志',self.logs));groups['依据复查'].addWidget(button('查看聚水潭分配依据',lambda:self.jst_action(detail)))
        groups['维护'].addWidget(button('更正已锁订单',lambda:self.jst_action(lambda p,h,s:change_lock(p,h,s,True))));groups['维护'].addWidget(button('撤销未下单生成单',lambda:self.jst_action(cancel_orders)))
        groups['流程处理'].addWidget(import_group(self,host,refresh=self.imported,scope_provider=lambda:list(self.checked.values())));v.addWidget(label('拖入多张订单或多个环节单据；有勾选时严格限定在所选订单。','muted'));self.selection_label();self.setAcceptDrops(True)
    def imported(self):
        self.query(keep_page=True);self.host.status.setText('所选生命周期单据已保存；原签发未改变。')
    def jst_action(self,fn):
        heads=sorted(self.checked.values(),key=lambda h:h['list_position'])
        if not heads:QMessageBox.information(self,'请选择订单','请先勾选要操作的店仓订单。');return
        visible={service.key(h) for h in self.records};hidden=sum(service.key(h) not in visible for h in heads)
        if hidden:
            from interaction_ui import confirm
            if not confirm(self,'确认所选范围',f'共{len(heads)}单，其中{hidden}单不在当前页，是否仍操作全部所选？'):return
        fn(self,self.host,heads);self.query(keep_page=True)
    def logs(self):
        from jst_ui import Logs
        d=Logs(self,self.host,self.start.text().strip(),self.end.text().strip());d.exec();d.deleteLater()
    def dragEnterEvent(self,event):
        from pathlib import Path
        urls=event.mimeData().urls()
        if urls and all(u.isLocalFile() and Path(u.toLocalFile()).suffix.lower()=='.xlsx' for u in urls):event.acceptProposedAction()
    def dropEvent(self,event):
        from lifecycle_batch_ui import import_batch
        if import_batch(self,self.host,scope=list(self.checked.values()) or None,paths=[u.toLocalFile() for u in event.mimeData().urls()]):self.imported()
        event.acceptProposedAction()
    def export_signed(self):
        from business_ui import run
        from interaction_ui import confirm
        import lifecycle_batch,export_locations
        heads=list(self.checked.values())
        if not heads:QMessageBox.information(self,'请选择订单','先勾选要导出的订单。');return
        folder=export_locations.initial_directory(self)
        if not confirm(self,'导出签发单','每店每仓一份原签发文件，同名文件直接覆盖，不重新签发。\n'+'\n'.join(h['number'] for h in heads)+'\n导出位置：'+folder):return
        try:
            files=run(self,lifecycle_batch.export_signed,(self.host.work_repository.path,heads,folder),'导出签发单')
            QMessageBox.information(self,'签发单已导出','\n'.join(files))
        except Exception as exc:warn(self,exc)
    def query(self,*_,keep_page=False):
        if self.host.closing:return
        if not keep_page:self.page=0;self.checked.clear()
        self.selection_label();self.table.setEnabled(False);self.export_btn.setEnabled(False);self.bar.setRange(0,0);self.note.setText('正在查询单据进度…可继续使用工作页。')
        self.reader.request({'kind':'daily_batch','from':self.start.text().strip(),'to':self.end.text().strip(),'search':self.search.text().strip(),'status':'' if self.status.currentIndex()==0 else self.status.currentText(),'warehouse':'' if self.warehouse_filter.currentIndex()==0 else self.warehouse_filter.currentText(),'lock':'' if self.lock_filter.currentIndex()==0 else self.lock_filter.currentText(),'page':self.page},self.ready,self.failed,self.progress)
    def progress(self,p):self.bar.setRange(0,max(1,p['total']));self.bar.setValue(p['done']);self.note.setText(f"读取归档 {p['done']} / {p['total']}")
    def ready(self,data):
        self.records=data['records'];self.page=data['page'];self.count=data['count'];self.bar.setRange(0,1);self.bar.setValue(1);self.table.setEnabled(True);self.note.setText(f"查询范围 {data['scope']} 单 · 当前筛选 {data['count']} 单 · 第 {self.page+1}/{max(1,(data['count']+49)//50)} 页；每页50单，阶段计数不重复。")
        for k,b in self.cards.items():b.setText(k+'\n'+str(data['counts'][k]))
        blocker=QSignalBlocker(self.table);self.table.setRowCount(len(self.records))
        for i,h in enumerate(self.records):
            c=QTableWidgetItem();c.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsUserCheckable);c.setCheckState(Qt.CheckState.Checked if service.key(h) in self.checked else Qt.CheckState.Unchecked);self.table.setItem(i,0,c)
            notices=[h['note']] if h.get('note') else []
            if h.get('locked'):notices.append('已下单锁定：后续不再分配')
            if h.get('generation_summary'):notices.append('聚水潭生成 '+h['generation_summary']['amount']+' 元')
            if h['legacy_incomplete']:notices.append('已有旧回传资料，请导入完整确认单')
            if h['old_revision']:notices.append('旧签发修订另有下游资料，未转移至本修订')
            if h['warehouse']=='旧版未标仓':notices.append('仓归属待核，不能自动关联')
            values=[h['day'],h['name']+'（'+h['store']+'）',h['warehouse'],h['number'],h['status'],*[('已导入' if h['present'][s] else '未导入') for s in service.STAGES],'；'.join(notices)]
            for j,x in enumerate(values,3):self.table.setItem(i,j,QTableWidgetItem(str(x)))
            self.table.setCellWidget(i,1,button('查看订单详情',lambda checked=False,r=h:self.open_comparison(r)))
            s=h['next'];recheck=s and h['present'].get(s)
            title='生成聚水潭订单' if s=='jst_generate' else '核对付款草稿' if s=='jst_payment' else ('重新核对' if recheck else '导入')+TITLES[s] if s else '无后续待办'
            b=button(title,lambda checked=False,r=h:self.next(r));b.setEnabled(s is not None and h['warehouse']!='旧版未标仓');self.table.setCellWidget(i,2,b)
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
        if s in ('jst_generate','jst_payment'):
            from jst_ui import generate,payment
            if s=='jst_generate':generate(self,self.host,[head])
            else:payment(self,self.host,head)
            self.query(keep_page=True);return
        result=review_again(self,self.host,head,s) if head['present'][s] else import_document(self,self.host,s,head)
        if result:self.query(keep_page=True)
    def shutdown(self):self.reader.shutdown()
