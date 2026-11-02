"""Home shows the earliest missing document, never an editable follow-up state."""
from datetime import date,timedelta
from PySide6.QtCore import Qt,QSignalBlocker,QTimer
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QLineEdit,QComboBox,QTableWidget,QTableWidgetItem,QProgressBar,QMessageBox,QTabWidget,QDialog
from ui_controls import button,label
from history_tasks import HistoryReader
import work_repository as wr
import order_lifecycle as service
import daily_batch
from lifecycle_ui import LifecycleDialog,import_document,import_group,export_selected,warn,review_again
from lifecycle_io import TITLES

class LifecycleWidget(QWidget):
    STAGES={'return':'回传处理','jst':'聚水潭导入单','payment':'付款核对','receipt':'收货核对'}
    def __init__(self,host,stage=None,scope=None):
        super().__init__(host);self.host=host;self.stage=stage;self.fixed_scope=scope;self.records=[];self.page=0;self.checked={};self.reader=HistoryReader(host.work_repository.path,host.repository.folder/'lifecycle_index.sqlite3',self)
        v=QVBoxLayout(self);v.setContentsMargins(0,0,0,0);v.addWidget(label(self.STAGES.get(stage,'日批次总览 · 看进度，进入对应环节处理'),'section'));filters=QHBoxLayout();self.start=QLineEdit(wr.today());self.end=QLineEdit(wr.today())
        for e in (self.start,self.end):e.setMaximumWidth(120);e.setPlaceholderText('YYYY-MM-DD')
        filters.addWidget(label('业务日期'));filters.addWidget(self.start);filters.addWidget(label('至'));filters.addWidget(self.end);self.search=QLineEdit();self.search.setPlaceholderText('门店 / 仓 / 单号');self.search.setClearButtonEnabled(True);filters.addWidget(self.search,1);filters.addWidget(button('查询',self.query));filters.addWidget(button('全部未完成（不限日期）',self.all_pending));v.addLayout(filters)
        self.status=QComboBox();self.status.addItems((['待生成','已生成未锁','已锁','本环节全部'] if stage=='jst' else ['待处理','本环节全部']) if stage else ['全部记录','全部未完成',*daily_batch.STATUSES]);row=QHBoxLayout();row.addWidget(self.status);self.warehouse_filter=QComboBox();self.warehouse_filter.addItems(['全部仓','河北仓','山东仓']);row.addWidget(self.warehouse_filter);self.lock_filter=QComboBox();self.lock_filter.addItems(['全部锁状态','已锁','未锁']);row.addWidget(self.lock_filter);row.addWidget(label('一天一批，各单独立推进；锁单不等于付款。','muted'),1);v.addLayout(row);self.status.currentIndexChanged.connect(self.query);self.warehouse_filter.currentIndexChanged.connect(self.query);self.lock_filter.currentIndexChanged.connect(self.query);self.search.returnPressed.connect(self.query)
        self.cards={};row=QHBoxLayout()
        for title in daily_batch.STATUSES:
            key={'待回传':'return','待付款':'payment','待收货':'receipt'}.get(title)
            b=button(title+'\n—',lambda checked=False,s=title,k=key:self.open_stage(k) if k else self.status.setCurrentText(s));self.cards[title]=b;row.addWidget(b);b.setVisible(stage is None)
        v.addLayout(row);self.bar=QProgressBar();self.bar.setMaximumHeight(14);v.addWidget(self.bar);self.note=label('待查询','muted');v.addWidget(self.note)
        self.table=QTableWidget();self.table.setColumnCount(12);self.table.setHorizontalHeaderLabels(['勾选','单据详情','下一步','业务日期','门店','仓','补货单号','当前进度','门店回传','门店付款','实际收货','资料提示']);self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);self.table.itemChanged.connect(self.check);v.addWidget(self.table,1)
        if stage:
            for column in (8,9,10):self.table.setColumnHidden(column,True)
        row=QHBoxLayout();row.addWidget(button('勾选本页',lambda:self.select_page(True)));row.addWidget(button('清空勾选',lambda:self.select_page(False)));self.export_btn=button('导出勾选单生命周期对照',self.export);row.addWidget(self.export_btn);row.addWidget(button('上一页',lambda:self.turn(-1)));row.addWidget(button('下一页',lambda:self.turn(1)));self.selected_label=label('');row.addWidget(self.selected_label);v.addLayout(row)
        self.actions=QTabWidget();self.primary_actions=[]
        if stage is None:
            row=QHBoxLayout()
            for key,title in self.STAGES.items():
                if key!='jst':row.addWidget(button('进入'+title,lambda checked=False,k=key:self.open_stage(k)))
            row.addWidget(button('聚水潭导入单',lambda:self.open_stage('jst')))
            v.addLayout(row);row=QHBoxLayout();row.addWidget(button('批次作业日志',self.logs));row.addWidget(button('导出勾选签发单',self.export_signed));row.addWidget(button('批量导入混合环节单据',lambda:self.import_stage(None)));v.addLayout(row)
            from data_maintenance_ui import remove
            row.addWidget(button('彻底删除勾选订单',lambda:remove(self,self.host,list(self.checked.values()))))
            self.actions.hide()
            v.addWidget(label('总览只看进度；按环节进入工作页。勾选后带入所选订单；聚水潭导入单默认本日全部，不继承筛选。其他环节沿用日期、仓和搜索。','muted'))
        else:
            row=QHBoxLayout()
            def primary(title,fn):
                b=button(title,fn,True);self.primary_actions.append(b);row.addWidget(b)
            if stage=='jst':
                from jst_ui import generate,export_orders,change_lock,cancel_orders,detail
                primary('生成勾选聚水潭订单',lambda:self.jst_action(generate))
                row.addWidget(button('重试勾选未完成生成',self.retry_generation))
                row.addWidget(button('导出勾选聚水潭订单',lambda:self.jst_action(export_orders)))
                row.addWidget(button('确认勾选已下单并锁定',lambda:self.jst_action(change_lock)))
                v.addLayout(row);secondary=QHBoxLayout();secondary.addWidget(button('查看分配依据',lambda:self.jst_action(detail)));secondary.addWidget(button('批次作业日志',self.logs));v.addLayout(secondary)
                maintenance=QWidget();m=QHBoxLayout(maintenance);m.addWidget(button('更正已锁订单',lambda:self.jst_action(lambda p,h,s:change_lock(p,h,s,True))));m.addWidget(button('撤销未下单生成单',lambda:self.jst_action(cancel_orders)));maintenance.hide()
                b=button('展开更正与撤销',lambda:maintenance.setVisible(not maintenance.isVisible()));v.addWidget(b);v.addWidget(maintenance)
            else:
                primary('导入'+TITLES[stage],lambda:self.import_stage(stage))
                row.addWidget(button('导出'+TITLES[stage],lambda:export_selected(self,self.host,list(self.checked.values()),stage)))
                if stage=='payment':row.addWidget(button('使用聚水潭导入单作为付款来源',self.review_payment))
                if stage=='payment':row.addWidget(button('勾选单批量生成翱象采购预览',self.aoxiang_prepare))
                if stage=='receipt':row.addWidget(button('勾选单批量同步翱象收货',self.aoxiang_receipts))
                if stage=='return':row.addWidget(button('导出原签发单',self.export_signed))
                if stage in ('return','payment'):row.addWidget(button('聚水潭导入单',lambda:self.open_stage('jst')))
                v.addLayout(row)
            self.actions.hide();v.addWidget(label('本页只处理'+self.STAGES[stage]+'。已完成后可切换“本环节全部”查看或更正；下一环节从总览进入。','muted'))
        self.selection_label();self.setAcceptDrops(True)
    def import_stage(self,stage):
        from lifecycle_batch_ui import import_batch
        if import_batch(self,self.host,stage,scope=list(self.checked.values()) or self.fixed_scope):self.imported()
    def review_payment(self):
        from jst_ui import payment
        heads=list(self.checked.values())
        if len(heads)!=1:QMessageBox.information(self,'选择一张付款草稿','请勾选一张订单核对实际付款；多张实际付款单可使用批量导入。');return
        payment(self,self.host,heads[0]);self.query(keep_page=True)
    def retry_generation(self):
        from business_ui import run
        import jst_tasks
        heads=list(self.checked.values())
        if not heads:QMessageBox.information(self,'请选择订单','先勾选需要重试的订单；已有生成单不会重新分配。');return
        try:
            run(self,jst_tasks.retry,(self.host.work_repository.path,heads),'记录生成重试')
            self.host.jst_generator.start();self.query(keep_page=True)
        except Exception as exc:warn(self,exc)
    def open_stage(self,stage,heads=None,use_selection=True):
        if stage is None:return
        d=StageDialog(self,stage,heads if heads is not None else (list(self.checked.values()) or None) if use_selection else None)
        previous=getattr(self.host,'_large_io_dialog',None)
        if stage=='jst':self.host._large_io_dialog=d
        try:d.exec()
        finally:
            if stage=='jst':self.host._large_io_dialog=previous
            d.deleteLater()
        self.query(keep_page=True)
        if self.host.closing:QTimer.singleShot(0,self.host.close)
    def imported(self):
        self.query(keep_page=True);self.host.status.setText('所选生命周期单据已保存；原签发未改变。')
        if hasattr(self.host,'jst_generator'):self.host.jst_generator.start()
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
        if self.stage=='jst':QMessageBox.information(self,'更新仓资料','先点“生成勾选聚水潭订单”，在分配窗口选择仓后拖入对应资料，避免混淆仓。');return
        if import_batch(self,self.host,self.stage,scope=list(self.checked.values()) or self.fixed_scope,paths=[u.toLocalFile() for u in event.mimeData().urls()]):self.imported()
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
            from readable_ui import file_results
            file_results(self,files,'签发单已导出')
        except Exception as exc:warn(self,exc)
    def query(self,*_,keep_page=False):
        if self.host.closing:return
        if not keep_page:self.page=0;self.checked.clear()
        self.selection_label();self.table.setEnabled(False);self.export_btn.setEnabled(False);self.bar.setRange(0,0);self.note.setText('正在查询单据进度…可继续使用工作页。')
        self.reader.request({'kind':'daily_batch','from':self.start.text().strip(),'to':self.end.text().strip(),'search':self.search.text().strip(),'status':'' if self.stage or self.status.currentIndex()==0 else self.status.currentText(),'stage_view':self.stage,'stage_filter':self.status.currentText() if self.stage else None,'scope':[[h['oid'],h['warehouse']] for h in self.fixed_scope] if self.fixed_scope is not None else None,'warehouse':'' if self.warehouse_filter.currentIndex()==0 else self.warehouse_filter.currentText(),'lock':'' if self.lock_filter.currentIndex()==0 else self.lock_filter.currentText(),'page':self.page},self.ready,self.failed,self.progress)
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
            if h.get('generation_task'):notices.append(h['generation_task']['message'])
            if h['legacy_incomplete']:notices.append('已有旧回传资料，请导入完整确认单')
            if h['old_revision']:notices.append('旧签发修订另有下游资料，未转移至本修订')
            if h['warehouse']=='旧版未标仓':notices.append('仓归属待核，不能自动关联')
            values=[h['day'],h['name']+'（'+h['store']+'）',h['warehouse'],h['number'],h['status'],*[('已导入' if h['present'][s] else '未导入') for s in service.STAGES],'；'.join(notices)]
            for j,x in enumerate(values,3):self.table.setItem(i,j,QTableWidgetItem(str(x)))
            self.table.setCellWidget(i,1,button('查看订单详情',lambda checked=False,r=h:self.open_comparison(r)))
            s=h['next'];recheck=s and h['present'].get(s)
            title='生成聚水潭订单' if s=='jst_generate' else '核对付款草稿' if s=='jst_payment' else ('重新核对' if recheck else '导入')+TITLES[s] if s else '无后续待办'
            if self.stage:
                applicable=s in ({'jst_generate'} if self.stage=='jst' else {'payment','jst_payment'} if self.stage=='payment' else {self.stage})
                if not applicable:title='本环节已处理'
            else:
                applicable=s is not None
                if s:title='进入'+self.STAGES[{'jst_generate':'jst','jst_payment':'payment'}.get(s,s)]
            b=button(title,lambda checked=False,r=h:self.next(r));b.setEnabled(applicable and h['warehouse']!='旧版未标仓');self.table.setCellWidget(i,2,b)
        del blocker
        for i,width in enumerate([45,110,165,100,185,70,250,155,90,90,90,260]):self.table.setColumnWidth(i,width)
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
    def all_pending(self):
        self.start.clear();self.end.clear();b=QSignalBlocker(self.status)
        if self.stage:self.status.setCurrentIndex(0)
        else:self.status.setCurrentText('全部未完成')
        del b;self.query()
    def export(self):export_selected(self,self.host,sorted(self.checked.values(),key=lambda h:h['list_position']))
    def open_comparison(self,head):
        try:self.host.open_archive(head,'lifecycle');self.query(keep_page=True)
        except Exception as exc:warn(self,exc)
    def next(self,head):
        s=head['next']
        if not s:return
        if not self.stage:self.open_stage({'jst_generate':'jst','jst_payment':'payment'}.get(s,s),[head]);return
        if s in ('jst_generate','jst_payment'):
            from jst_ui import generate,payment
            if s=='jst_generate':generate(self,self.host,[head])
            else:payment(self,self.host,head)
            self.query(keep_page=True);return
        result=review_again(self,self.host,head,s) if head['present'][s] else import_document(self,self.host,s,head)
        if result:self.query(keep_page=True)
    def shutdown(self):self.reader.shutdown()

    def aoxiang_prepare(self):
        from aoxiang_workflow_ui import prepare_selected
        prepare_selected(self,self.host,sorted(self.checked.values(),key=lambda h:h['list_position']))

    def aoxiang_receipts(self):
        if not self.checked:QMessageBox.information(self,'先选择范围','请先勾选要同步的仓单。');return
        from aoxiang_workflow_ui import open_receipt_selection
        open_receipt_selection(self,self.host,sorted(self.checked.values(),key=lambda h:h['list_position']))


class StageDialog(QDialog):
    """Focused task workspace; overview state remains in its existing widget."""
    def __init__(self,overview,stage,scope=None):
        super().__init__(overview);self.overview=overview;self.stage=stage;self._completed=False;self.setWindowTitle(LifecycleWidget.STAGES[stage]+' · 环节工作页');self.resize(1360,850)
        v=QVBoxLayout(self);v.addWidget(button('← 返回订单跟踪总览',self.reject))
        if stage=='jst':
            from jst_results_ui import ResultsPage
            self.setProperty('jstResults',True);self.panel=ResultsPage(overview.host,scope,self);v.addWidget(self.panel,1);return
        v.addWidget(label('限定所选 '+str(len(scope))+' 单' if scope is not None else '沿用总览日期、仓及搜索；本页筛选不改总览。','muted'))
        self.panel=LifecycleWidget(overview.host,stage,scope);v.addWidget(self.panel,1)
        for target,source in ((self.panel.start,overview.start),(self.panel.end,overview.end),(self.panel.search,overview.search)):target.setText(source.text())
        for target,source in ((self.panel.warehouse_filter,overview.warehouse_filter),(self.panel.lock_filter,overview.lock_filter)):
            blocker=QSignalBlocker(target);target.setCurrentIndex(source.currentIndex());del blocker
        if scope:
            self.panel.checked={service.key(h):h for h in scope};blocker=QSignalBlocker(self.panel.status);self.panel.status.setCurrentIndex(self.panel.status.count()-1);del blocker
        QTimer.singleShot(0,lambda:self.panel.query(keep_page=True))
    def done(self,result):
        if self.stage=='jst' and self.panel.worker:
            self.panel.close_pending=result;self.panel.stop();return
        self.panel.shutdown();self._completed=True;super().done(result)
    def closeEvent(self,event):
        self.reject()
        if self._completed:event.accept()
        else:event.ignore()
