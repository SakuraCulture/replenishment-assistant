"""One persistent, store-first result page; export actions never allocate stock."""
import copy
from pathlib import Path
from PySide6.QtCore import Qt,QDate,QTimer,QSignalBlocker,QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QDateEdit,QLineEdit,QComboBox,
    QTreeWidget,QTreeWidgetItem,QProgressBar,QDialog,QMessageBox,QInputDialog)
from ui_controls import label,button
from ui_tasks import Worker
from history_tasks import HistoryReader
from business_ui import run
from interaction_ui import confirm
from jst_review_ui import SummaryLabel,AllocationReview
import jst_review as review
import jst_results as service
import jst_batches as batches
import export_locations
import work_repository as wr

ROLE=Qt.ItemDataRole.UserRole
CHECKED=Qt.CheckState.Checked


class ResultsPage(QWidget):
    def __init__(self,host,scope=None,parent=None,*,order_kind='normal_model'):
        super().__init__(parent);self.host=host;self.path=host.work_repository.path;self.scope=scope
        if scope and any(h.get('kind')=='material' for h in scope):
            order_kind = 'material' if all(h.get('kind')=='material' for h in scope) else 'all'
        self.order_kind = order_kind
        self.data=None;self.records=[];self.count=0;self.worker=None;self.reading=False;self.closing=False;self.reviewing=False
        self.batch_id=None;self.close_pending=None;self.last_plan=None;self.remaining=[];self.chosen=set();self.building=False;self.payment_notice=''
        self.reader=HistoryReader(self.path,host.repository.folder/'jst_result_index.sqlite3',self)
        v=QVBoxLayout(self);v.setContentsMargins(0,0,0,0)
        v.addWidget(label('聚水潭导入单 · 看分配结果，随时导出','section'))
        row=QHBoxLayout();row.addWidget(label('业务日期'));self.day=QDateEdit();self.day.setCalendarPopup(True);self.day.setDisplayFormat('yyyy-MM-dd')
        self.day.setDate(QDate.fromString(min(h['day'] for h in scope) if scope else wr.today(),'yyyy-MM-dd'));row.addWidget(self.day)
        self.end=QDateEdit();self.end.setCalendarPopup(True);self.end.setDisplayFormat('yyyy-MM-dd');self.end.setDate(QDate.fromString(max(h['day'] for h in scope) if scope else wr.today(),'yyyy-MM-dd'));row.addWidget(label('至'));row.addWidget(self.end)
        self.refresh_btn=button('刷新列表',self.refresh);row.addWidget(self.refresh_btn)
        self.scope_btn=button('解除所选单限制',self.clear_scope);row.addWidget(self.scope_btn);self.scope_btn.setVisible(scope is not None)
        row.addStretch();self.prepare_btn=button('准备新分配批次',self.prepare);row.addWidget(self.prepare_btn);v.addLayout(row)
        self.scope_note=label('','muted');v.addWidget(self.scope_note)
        row=QHBoxLayout();self.batch_btn=button('选择分配批次',self.choose_batch);row.addWidget(self.batch_btn)
        self.all_batches_btn=button('返回日期范围',self.clear_batch);row.addWidget(self.all_batches_btn)
        self.continue_btn=button('继续原批未完成任务',self.continue_batch);row.addWidget(self.continue_btn)
        self.recovery_btn=button('恢复分配状态',self.recover_state);row.addWidget(self.recovery_btn)
        self.reallocate_btn=button('重新分配本批勾选单',self.reallocate);row.addWidget(self.reallocate_btn);v.addLayout(row)
        self.total=SummaryLabel('正在读取已保存结果…');v.addWidget(self.total)
        self.counts=label('','muted');v.addWidget(self.counts)
        row=QHBoxLayout();self.search=QLineEdit();self.search.setPlaceholderText('门店 / 仓 / 补货单号 / 聚水潭内部订单号 / 翱象采购单号');self.search.setClearButtonEnabled(True);row.addWidget(self.search,1)
        self.warehouse=QComboBox();self.warehouse.addItems(['全部仓','河北仓','山东仓']);row.addWidget(self.warehouse)
        row.addWidget(button('勾选筛选结果',self.select_visible));row.addWidget(button('清空勾选',self.clear_selection));v.addLayout(row)
        v.addWidget(label('展开门店可单选仓单；双击查看商品。导出和新建批次只处理当前筛选中的订单，数量在按钮上显示。','muted'))
        self.table=QTreeWidget();self.table.setColumnCount(7);self.table.setHeaderLabels(['门店 / 仓单','准备情况','需求金额','已分配金额','减少金额','减量SKU','最近导出'])
        self.table.setAlternatingRowColors(True);self.table.setUniformRowHeights(True);self.table.setSortingEnabled(False)
        for i,width in enumerate((320,200,110,110,110,85,160)):self.table.setColumnWidth(i,width)
        self.table.itemChanged.connect(self.checked);self.table.itemDoubleClicked.connect(self.detail);v.addWidget(self.table,1)
        self.selection=label('已勾选0单，可导出0份','muted');v.addWidget(self.selection)
        folder=QHBoxLayout();folder.addWidget(label('导出位置'));self.folder=QLineEdit(export_locations.initial_directory(self));self.folder.setReadOnly(True);self.folder.setAccessibleName('本次导出文件夹');folder.addWidget(self.folder,1)
        self.folder_btn=button('更改本次目录',self.choose_folder);folder.addWidget(self.folder_btn);v.addLayout(folder)
        self.progress=QProgressBar();self.progress.setRange(0,1);v.addWidget(self.progress)
        self.status=label('正在读取结果…','muted');v.addWidget(self.status)
        row=QHBoxLayout();self.all_btn=button('导出本批全部（0份）',self.export_all,True);self.selected_btn=button('导出勾选（0份）',self.export_selected)
        self.open_btn=button('打开导出文件夹',self.open_folder);row.addWidget(self.all_btn);row.addWidget(self.selected_btn);row.addWidget(self.open_btn);v.addLayout(row)
        self.evidence_btn=button('导出当前筛选采用仓资料（0单）',self.export_stock_evidence);self.evidence_btn.setObjectName('exportSavedStockEvidence');self.evidence_btn.setToolTip('导出已保存分配实际使用的商品资料和库存依据；不同批次分别标识，不用当前默认资料替换。');v.addWidget(self.evidence_btn)
        row=QHBoxLayout();self.payment_all_btn=button('按当前筛选核对付款（0单）',self.payment_all)
        self.payment_selected_btn=button('按勾选核对付款（0单）',self.payment_selected)
        row.addWidget(self.payment_all_btn);row.addWidget(self.payment_selected_btn);v.addLayout(row)
        row=QHBoxLayout()
        self.one_click_btn=button('一键分配下单',self.one_click)
        self.one_click_btn.setObjectName('jstOneClick')
        self.one_click_btn.setToolTip('核对所勾选仓单的历史配置；获取对应仓最新库存，分配后按平台预览导入并查回原单。')
        self.auto_prepare_btn=button('准备自动下单',self.auto_prepare)
        self.auto_tasks_btn=button('自动下单任务',self.auto_tasks)
        self.auto_profiles_btn=button('买家和收货资料',self.auto_profiles)
        row.addWidget(self.one_click_btn);row.addWidget(self.auto_prepare_btn);row.addWidget(self.auto_tasks_btn);row.addWidget(self.auto_profiles_btn);v.addLayout(row)
        row=QHBoxLayout();self.retry_btn=button('重试未完成导出',self.retry);self.stop_btn=button('停止本次导出',self.stop)
        row.addWidget(self.retry_btn);row.addWidget(self.stop_btn);self.tools_btn=button('其他订单操作',self.tools);row.addWidget(self.tools_btn);v.addLayout(row)
        self.day.dateChanged.connect(self.day_changed);self.end.dateChanged.connect(self.day_changed);self.search.textChanged.connect(self.filter_changed);self.warehouse.currentIndexChanged.connect(self.filter_changed)
        self.timer=QTimer(self);self.timer.setInterval(3000);self.timer.timeout.connect(self.poll);self.timer.start()
        self.controls();QTimer.singleShot(0,self.refresh)

    def controls(self):
        busy=self.worker is not None or self.reviewing;ready=self.data is not None and not self.closing
        visible={service.key(r['head']) for r in self.visible_records()} if self.data else set()
        n=len(service.export_plan(self.data,visible)['entries']) if self.data else 0
        m=len(service.export_plan(self.data,self.chosen)['entries']) if self.data else 0
        self.all_btn.setText(f'导出当前筛选（{n}份）');self.selected_btn.setText(f'导出勾选（{m}份）')
        self.all_btn.setEnabled(ready and not busy and n>0);self.selected_btn.setEnabled(ready and not busy and m>0)
        evidence_rows=self.stock_evidence_records()
        self.evidence_btn.setText('导出本批采用仓资料' if self.batch_id else ('导出勾选采用仓资料' if self.chosen else '导出当前筛选采用仓资料')+f'（{len(evidence_rows)}单）')
        self.evidence_btn.setEnabled(ready and not busy and (bool(self.batch_id) or bool(evidence_rows)))
        self.payment_all_btn.setText(f'按当前筛选核对付款（{len(self.visible_records())}单）');self.payment_selected_btn.setText(f'按勾选核对付款（{len(self.chosen)}单）')
        self.payment_all_btn.setEnabled(ready and not busy and bool(self.visible_records()));self.payment_selected_btn.setEnabled(ready and not busy and bool(self.chosen))
        self.auto_prepare_btn.setText(f'准备勾选单自动下单（{len(self.chosen)}单）')
        self.auto_prepare_btn.setEnabled(ready and not busy and bool(self.chosen))
        chosen=len(self.chosen & visible)
        self.one_click_btn.setText(f'一键分配下单（{chosen}单）')
        self.one_click_btn.setEnabled(ready and not self.reading and not busy and chosen>0 and not getattr(self.host,'closing',False))
        self.auto_profiles_btn.setEnabled(not busy and not self.closing)
        self.auto_tasks_btn.setEnabled(not busy and not self.closing)
        for b in (self.payment_all_btn,self.payment_selected_btn):b.setToolTip('直接使用已保存生成单，无需导出再导入；核对实际已付款后才保存。')
        self.selection.setText(f'已勾选 {len(self.chosen)} 单，可导出 {m} 份')
        for b in (self.day,self.end,self.refresh_btn,self.scope_btn,self.folder_btn,self.prepare_btn,self.tools_btn,self.batch_btn,self.all_batches_btn):b.setEnabled(not busy and not self.closing)
        self.day.setEnabled(not busy and not self.batch_id);self.end.setEnabled(not busy and not self.batch_id)
        correction_rows=[r for r in self.records if service.key(r['head']) in self.chosen and r.get('batch_id') and (r.get('gid') or r.get('original_zero'))]
        self.continue_btn.setEnabled(ready and not busy and bool(self.batch_id) and bool(self.data.get('batch_pending')))
        self.recovery_btn.setEnabled(ready and not busy and bool(self.batch_id))
        self.reallocate_btn.setText(f'按原批重新分配勾选单（{len(correction_rows)}单）')
        self.reallocate_btn.setEnabled(ready and not busy and bool(correction_rows))
        self.reallocate_btn.setToolTip('沿用原批库存，替换所选可更正仓单。旧付款依据因回传变化失效时，分配后按新结果重新核对付款。多个原批分别处理。')
        self.continue_btn.setToolTip('先选择分配批次；继续使用其冻结库存和原顺序')
        if not n:self.all_btn.setToolTip('当前筛选没有有效生成单；请准备批次或核对待处理原因')
        self.stop_btn.setEnabled(self.worker is not None and not self.worker.cancel.is_set());self.retry_btn.setEnabled(bool(self.remaining) and not busy and not self.closing)

    def refresh(self):
        if self.closing or self.worker or self.reviewing:return
        self.reading=True;self.controls()
        self.scope_note.setText(('分配批次 '+self.batch_id[:8]+' · 冻结库存、原分配顺序' if self.batch_id else self.day.text()+' 至 '+self.end.text()+' · 日期范围（可包含多个分配批次）')+(' · 限定所选 '+str(len(self.scope))+' 单' if self.scope is not None else ''))
        if self.data is None:self.progress.show();self.progress.setRange(0,0)
        self.reader.request({'kind':'jst_results','day':self.day.text(),'date_end':self.end.text(),'batch_id':self.batch_id,'scope':[list(service.key(h)) for h in self.scope] if self.scope is not None else None,'platform_tracking':True,'order_kind':self.order_kind},self.ready,self.failed_read,self.read_progress)
    def poll(self):
        if self.isVisible() and not self.worker and not self.reading and not self.closing and not self.reviewing:self.refresh()
    def read_progress(self,p):
        if self.data is None:self.progress.setRange(0,max(1,p['total']));self.progress.setValue(p['done']);self.status.setText(p['text'])
    def failed_read(self,message):
        self.reading=False;self.data=None;self.records=[];self.count=0;self.chosen.clear();self.table.clear();self.total.setText('结果读取失败，请刷新');self.status.setText(message);self.progress.setRange(0,1);self.controls()
    def ready(self,data):
        if self.closing:return
        changed=data!=self.data;old={r['gid'] for r in self.records if r['state']=='ready'};new={r['gid'] for r in data['records'] if r['state']=='ready'}
        self.reading=False;self.data=data;self.records=data['records'];self.count=len(self.records)
        if data.get('batch_label'):self.scope_note.setText('分配批次 '+data['batch_label']+' · 冻结库存、原分配顺序')
        self.chosen.intersection_update(service.key(r['head']) for r in self.records)
        c=data['counts']
        if changed:self.render()
        if self.last_plan and new-old:self.status.setText(f'新增 {len(new-old)} 份可导出结果；不会加入已开始的导出，可再次导出。')
        elif self.last_plan is None:self.status.setText('查询完成；业务进度见各单状态。可重复导出有效结果，不重新分配。' if c['ready'] else '旧分配已失效，不能直接导出；请勾选要更正的订单，点击“按原批重新分配勾选单”。旧付款保留，新分配后需重新核对付款。' if any((r.get('gid') or r.get('original_zero')) and r['state']=='stale' for r in self.records) else '当前范围没有可导出结果：尚未回传请先确认回传；已回传请准备分配批次，或选择原批继续。')
        if self.payment_notice:self.status.setText(self.payment_notice)
        if any(r.get('withdrawn') and service.key(r['head']) in self.chosen for r in self.records):
            self.status.setText('已勾选撤销完成的仓单。点「准备新分配批次」，按仓刷新资料，再预览并确认生成。')
        if getattr(self,'redo_target',None) in self.chosen:
            self.status.setText('已返回重做仓单。点「准备新分配批次」按仓刷新资料；改量请先在订单跟踪更正本仓回传。')
        self.progress.hide();self.controls()
        if getattr(self,'auto_payment_scope',None):QTimer.singleShot(0,self.prepare_payment_automatically)

    def prepare_payment_automatically(self):
        scope=getattr(self,'auto_payment_scope',None)
        if not scope or self.closing or self.worker or self.reviewing or self.reading:return
        entries=[r for r in self.records if service.key(r['head']) in scope]
        if {service.key(r['head']) for r in entries}!=scope:return
        if any(r['state'] not in ('ready','zero') for r in entries):return
        self.auto_payment_scope=None
        ready=[r for r in entries if r['state']=='ready']
        if not ready:self.status.setText('本范围分配为0，无可转入付款的生成单；结果已保留。');return
        self.reviewing=True;self.controls()
        try:
            from jst_payment_ui import review
            result=review(self,self.host,ready)
            self.status.setText('付款已保存；可进入收货核对。' if result else '已准备付款核对；本次未确认付款，分配结果保留，可再次点击核对付款。')
        except Exception as exc:self.status.setText('分配结果已保留；付款准备未完成：'+str(exc)+'。可点击核对付款重试。')
        finally:self.reviewing=False;self.controls()
    def day_changed(self,*_):
        self.reader.cancel();self.data=None;self.records=[];self.table.clear();self.clear_selection();self.remaining=[];self.last_plan=None;self.refresh()
    def clear_scope(self):self.scope=None;self.scope_btn.hide();self.day_changed()
    def clear_selection(self):self.chosen.clear();self.render();self.controls()
    def filter_changed(self,*_):self.chosen.clear();self.render();self.controls();self.status.setText('筛选已更新并清空勾选；导出和新建批次只处理当前可见范围。')

    def visible_records(self):
        from order_tracking_refs import matches
        terms=self.search.text().lower().split();wh=self.warehouse.currentText() if self.warehouse.currentIndex() else ''
        return [r for r in self.records if (not wh or r['head']['warehouse']==wh) and (not terms or matches(r['head'],terms,('name','store','warehouse','number')))]

    def render(self):
        if self.data is None:return
        visible_rows=self.visible_records()
        self.total.setText('当前筛选：'+review.caption(service.aggregate([r for r in visible_rows if r['state'] in ('ready','zero')])))
        self.counts.setText(f"查询范围 {len(self.records)} 单；当前筛选 {len(visible_rows)} 单 / {len({r['head']['store'] for r in visible_rows})} 店 · 可导出 {sum(r['state']=='ready' for r in visible_rows)} 份；金额只计有效分配，缺价单列待核。")
        expanded={self.table.topLevelItem(i).data(0,ROLE) for i in range(self.table.topLevelItemCount()) if self.table.topLevelItem(i).isExpanded()}
        scroll=self.table.verticalScrollBar().value();block=QSignalBlocker(self.table);self.table.clear();self.building=True
        visible={service.key(r['head']) for r in self.visible_records()}
        for g in self.data['groups']:
            rs=[r for r in g['records'] if service.key(r['head']) in visible]
            if not rs:continue
            m=service.aggregate([r for r in rs if r['state'] in ('ready','zero')]);n=sum(r['state']=='ready' for r in rs)
            top=QTreeWidgetItem([g['name']+'（'+g['store']+'）',f'{n}/{len(rs)} 仓单可导出',*[review.amount_text(m,k) if any(r['projection'] and r['state'] in ('ready','zero') for r in rs) else '待处理' for k in ('demand','allocated','reduced')],str(m['reduced_sku']),max((r['last_export'] for r in rs if r['last_export'] not in ('—','未导出')),default='未导出')])
            self.table.addTopLevelItem(top);top.setData(0,ROLE,g['store']);top.setFlags(top.flags()|Qt.ItemFlag.ItemIsAutoTristate|Qt.ItemFlag.ItemIsUserCheckable)
            for r in rs:
                m=service.aggregate([r]);reason=r['reason']+(' · 已关联自动下单' if r.get('auto_internal') else ' · 有历史下单标记' if r['locked'] else '')
                child=QTreeWidgetItem([r['head']['warehouse']+' · '+r['head']['number'],reason,*[review.amount_text(m,k) if r['projection'] else '待处理' for k in ('demand','allocated','reduced')],str(m['reduced_sku']) if r['projection'] else '—',r['last_export']])
                top.addChild(child);child.setData(0,ROLE,list(service.key(r['head'])));child.setFlags(child.flags()|Qt.ItemFlag.ItemIsUserCheckable);child.setCheckState(0,CHECKED if service.key(r['head']) in self.chosen else Qt.CheckState.Unchecked)
                for c in range(self.table.columnCount()):child.setToolTip(c,child.text(c))
            top.setExpanded(g['store'] in expanded)
        self.table.verticalScrollBar().setValue(scroll);self.building=False;del block
    def checked(self,*_):
        if self.building:return
        self.chosen=set()
        for i in range(self.table.topLevelItemCount()):
            top=self.table.topLevelItem(i)
            for j in range(top.childCount()):
                child=top.child(j)
                if child.checkState(0)==CHECKED:self.chosen.add(tuple(child.data(0,ROLE)))
        self.controls()
    def select_visible(self):
        for i in range(self.table.topLevelItemCount()):self.table.topLevelItem(i).setCheckState(0,CHECKED)
    def detail(self,item,*_):
        if self.worker or not self.data:return
        k=item.data(0,ROLE);rs=[r for r in self.records if (service.key(r['head'])==tuple(k) if isinstance(k,list) else r['head']['store']==k)]
        gids=[r['gid'] for r in rs if r['gid']]
        if not gids:QMessageBox.information(self,'尚无可查看的生成单','；'.join(dict.fromkeys(r['reason'] for r in rs)));return
        try:
            values=run(self,service.detail,(self.path,gids),'读取已保存商品明细')
            d=QDialog(self);d.setProperty('jstResults',True);d.setWindowTitle('聚水潭导入单 · 商品明细');d.resize(1220,800);v=QVBoxLayout(d)
            v.addWidget(label('当时保存的分配结果；需核对的旧结果不会计入普通批量导出。','muted'));review_widget=AllocationReview(d);review_widget.replace(values,'所选门店已保存结果');v.addWidget(review_widget,1)
            if len(review_widget.groups)==1:review_widget.open_store(review_widget.store_model.index(0,0))
            actions=QHBoxLayout();actions.addWidget(button('返回结果页',d.reject))
            if len(rs)==1 and len(gids)==1:
                def amend():
                    from order_amendments_ui import open_editor
                    if open_editor(d,self.host,rs[0]['head'],'allocation'):d.accept()
                actions.addWidget(button('手动更正本仓分配',amend))
            v.addLayout(actions)
            changed=d.exec()==QDialog.DialogCode.Accepted;d.deleteLater()
            if changed:self.refresh()
        except Exception as exc:self.status.setText(str(exc))

    def stock_evidence_records(self):
        return [r for r in self.visible_records() if r.get('gid') and (not self.chosen or service.key(r['head']) in self.chosen)]

    def export_stock_evidence(self):
        if self.worker or self.reviewing or self.closing or not self.data:return
        import jst_stock_evidence as evidence
        from runtime_support import Cancelled
        from lifecycle_ui import warn
        if self.batch_id:
            fn,args=evidence.export_batch,(self.path,self.batch_id)
        else:
            gids=list(dict.fromkeys(r['gid'] for r in self.stock_evidence_records()))
            if not gids:
                self.status.setText('所选范围没有已保存分配依据；请先生成，或在预览窗口导出未确认预览采用的资料。');return
            fn,args=evidence.export_generations,(self.path,gids)
        target,_=export_locations.save_file(self,'导出实际采用仓资料','聚水潭实际采用仓资料.xlsx','Excel (*.xlsx)')
        if not target:self.status.setText('已取消选择导出位置；未导出文件。');return
        self.reviewing=True;self.reader.cancel();self.reading=False;self.controls()
        try:
            result=run(self,fn,(*args,target),'导出实际采用仓资料')
            self.status.setText('实际采用仓资料已导出：'+str(result))
        except Cancelled:self.status.setText('采用仓资料导出已取消；原分配和库存未改变。')
        except Exception as exc:
            self.status.setText('采用仓资料未导出：'+str(exc));warn(self,exc)
        finally:self.reviewing=False;self.controls()

    def export_all(self):
        if self.data:self.begin(service.export_plan(self.data,{service.key(r['head']) for r in self.visible_records()}))
    def export_selected(self):
        if self.data:self.begin(service.export_plan(self.data,self.chosen))
    def payment_all(self):self.review_payment(False)
    def payment_selected(self):self.review_payment(True)
    def review_payment(self,selected):
        if self.worker or self.reviewing or self.closing or not self.data:return
        import jst_payment,jst_payment_ui
        from lifecycle_ui import warn
        try:
            entries=jst_payment.selection(self.visible_records(),self.chosen if selected else None)
            self.reviewing=True;self.reader.cancel();self.reading=False;self.controls()
            receipt=jst_payment_ui.review(self,self.host,entries)
            self.payment_receipt=receipt
            if receipt is not None:self.payment_notice=f'付款核对完成 {len(receipt)} 单，新增或更新 {sum(r["changed"] for r in receipt)} 单。'
            else:self.payment_notice='已取消付款核对，本次未保存付款单。'
            self.status.setText(self.payment_notice)
        except Exception as exc:warn(self,exc)
        finally:
            self.reviewing=False;self.controls()
            if not self.closing:self.refresh()
    def begin(self,plan):
        if self.worker or self.closing or not plan['entries']:return
        self.reader.cancel();self.reading=False;self.last_plan=copy.deepcopy(plan);self.last_folder=self.folder.text();self.remaining=[]
        w=Worker(service.export,(self.path,self.last_plan,self.last_folder),self);self.worker=w
        w.status.connect(self.status.setText);w.row_progress.connect(self.export_progress);w.success.connect(self.export_done);w.failed.connect(self.export_failed);w.cancelled.connect(lambda:self.export_failed('导出已取消，尚未完成的原结果可重试'))
        w.finished.connect(self.export_finished);self.progress.setRange(0,len(plan['entries']));self.progress.setValue(0);self.status.setText(f"正在导出 {len(plan['entries'])} 份；范围已固定。")
        self.progress.show();self.controls();w.start()
    def export_progress(self,n,total):self.progress.setRange(0,max(1,total));self.progress.setValue(n)
    def export_done(self,r):
        self.remaining=r['remaining'];self.receipt=r
        messages=[f"成功 {len(r['files'])}/{r['total']} 份，未完成 {len(r['remaining'])} 份。可直接再次导出。"]
        heads={e['gid']:e['head'] for e in self.last_plan['entries']}
        messages.extend(heads[x['gid']]['name']+' / '+heads[x['gid']]['warehouse']+'：'+x['error'] for x in r['failed'])
        messages.extend(r.get('warnings',[]));self.status.setText('\n'.join(messages))
    def export_failed(self,message):
        self.remaining=[r['gid'] for r in self.last_plan['entries']];self.status.setText('导出未完成：'+message)
    def export_finished(self):
        w=self.worker;self.worker=None;w.deleteLater();self.controls()
        if self.close_pending is not None:QTimer.singleShot(0,lambda:self.window().done(self.close_pending))
        else:self.refresh()
    def retry(self):
        if not self.last_plan or not self.remaining:return
        p=copy.deepcopy(self.last_plan);p['entries']=[r for r in p['entries'] if r['gid'] in self.remaining];self.folder.setText(self.last_folder);self.begin(p)
    def stop(self):
        if self.worker:self.worker.cancel.set();self.status.setText('正在停止后续文件；已完成文件保留。');self.controls()
    def choose_folder(self):
        p=export_locations.directory(self,'本次导出目录',self.folder.text())
        if p:self.folder.setText(p)
    def open_folder(self):
        p=Path(self.folder.text())
        if p.is_dir():QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))
        else:self.status.setText('目录尚未创建；导出时自动创建。')
    def choose_batch(self):
        try:
            rows=run(self,batches.listing,(self.path,),'读取分配批次')
            if not rows:QMessageBox.information(self,'尚无分配批次','回传确认后，先点击“准备新分配批次”。');return
            titles=[r['label']+' · '+{'active':'进行中','paused':'已暂停','complete':'已处理'}.get(r['status'],r['status'])+' · '+r['batch_id'][:8] for r in rows]
            choice,ok=QInputDialog.getItem(self,'选择原分配批次','选择后查看该批全部订单，替代当前日期及勾选限制；继续、重分配均沿用原库存。',titles,0,False)
            if ok:self.batch_id=rows[titles.index(choice)]['batch_id'];self.scope=None;self.scope_btn.hide();self.day_changed()
        except Exception as exc:self.status.setText(str(exc))
    def clear_batch(self):self.batch_id=None;self.day_changed()
    def recover_state(self):
        if self.worker or self.reviewing or not self.batch_id:return
        from business_ui import run
        from lifecycle_ui import warn
        import jst_batches
        try:
            r=run(self,jst_batches.recover_batch,(self.host.work_repository.path,self.batch_id),'核对原批已保存结果')
            if self.host.closing:return
            self.refresh();self.status.setText(r['message'])
        except Exception as exc:warn(self,exc)
    def continue_batch(self):self.open_existing(False)
    def reallocate(self):self.open_existing(True)
    def open_existing(self,correction):
        if self.worker or self.reviewing:return
        if not correction and not self.batch_id:return
        import jst_ui
        self.reviewing=True;self.controls()
        try:
            import sqlite3
            from contextlib import closing
            bid=self.batch_id
            if correction:
                selected=[r for r in self.records if service.key(r['head']) in self.chosen]
                if not selected:QMessageBox.information(self,'请先勾选订单','勾选需要更正的门店或仓单，再按原批重新分配。');return
                missing=[r for r in selected if not r.get('batch_id') or not (r.get('gid') or r.get('original_zero'))]
                if missing:
                    QMessageBox.information(self,'所选订单需分别处理','以下订单没有可替换的原批生成单，请先取消这些勾选；未生成的订单使用“准备新分配批次”。\n'+'\n'.join(r['head']['number']+'：'+r['reason'] for r in missing));return
                groups={}
                for r in selected:groups.setdefault(r['batch_id'],[]).append(r)
                if bid is None:
                    ids=list(groups)
                    if len(ids)>1:
                        with closing(sqlite3.connect(self.path)) as db:contexts=[batches.context(db,i,load_stock=False) for i in ids]
                        titles=[c['label']+f" · 所选{len(groups[c['batch_id']])}单 · "+c['batch_id'][:8] for c in contexts]
                        choice,ok=QInputDialog.getItem(self,'选择本次要更正的原批','所选订单来自多个批次。每次仅更正一个原批，沿用其冻结库存；其他勾选保留。',titles,0,False)
                        if not ok:return
                        bid=ids[titles.index(choice)]
                    else:bid=ids[0]
            with closing(sqlite3.connect(self.path)) as db:c=batches.context(db,bid,load_stock=False)
            heads=[m['head'] for m in c['members'] if service.key(m['head']) in self.chosen and m['status']=='done'] if correction else [m['head'] for m in c['members'] if m['status'] in ('pending','failed')]
            if not heads:QMessageBox.information(self,'没有适用任务','本批没有未完成任务，或没有勾选可重新分配的已生成单；请展开仓单查看准备情况。');return
            jst_ui.generate(self,self.host,heads,bid,correction)
        except Exception as exc:self.status.setText(str(exc))
        finally:self.reviewing=False;self.controls();self.refresh()
    def prepare(self):
        if not self.data or self.worker:return
        import jst_ui
        rows=[r for r in self.visible_records() if (not self.chosen or service.key(r['head']) in self.chosen) and not r['gid'] and not r.get('original_zero') and not r['locked']]
        if not rows:
            selected=[r for r in self.visible_records() if service.key(r['head']) in self.chosen]
            if selected and all((r.get('gid') or r.get('original_zero')) and r.get('batch_id') for r in selected) and any(r['state']=='stale' for r in selected):self.reallocate();return
            QMessageBox.information(self,'没有可加入新批的订单','有效生成单可直接导出。回传已变化时，请勾选并按原批重新分配。已有下游单据时请先核对对应环节。');return
        jst_ui.generate(self,self.host,[r['head'] for r in rows]);self.refresh()
    def tools(self):
        if not self.data or self.worker:return
        import jst_ui
        selected=[r for r in self.records if service.key(r['head']) in self.chosen]
        heads=[r['head'] for r in selected]
        legacy_gids=[r['gid'] for r in selected if r['locked'] and r['gid'] and not r.get('auto_internal')]
        d=QDialog(self);d.setWindowTitle('其他订单操作');v=QVBoxLayout(d);v.addWidget(label(f'已勾选 {len(heads)} 张仓单'))
        def action(fn):
            if heads:fn(d,self.host,heads);d.accept()
            else:QMessageBox.information(d,'请先勾选','返回结果页勾选要操作的门店或仓单。')
        def withdraw_selected():
            if not heads:
                QMessageBox.information(d,'请先勾选','返回结果页勾选要核对的仓单。')
                return
            self.reviewing=True;self.reader.cancel();self.reading=False;self.controls()
            try:
                released = jst_ui.cancel_orders(d,self.host,heads)
                if released:
                    self.batch_id = None
                    self.chosen = {service.key(r['head']) for r in released}
                    self.last_plan = None
                    self.redo_target = None
                d.accept()
            finally:
                self.reviewing=False;self.controls()
        v.addWidget(button('撤销未下单生成单',withdraw_selected))
        v.addWidget(button('查看当时资料依据',lambda:action(jst_ui.detail)))
        if legacy_gids:
            def reconcile():
                if jst_ui.reconcile_legacy_orders(d,self.host,legacy_gids):d.accept()
            v.addWidget(button(f'核对历史下单标记（{len(legacy_gids)}单）',reconcile))
        def old_export():
            p=service.export_plan(self.data,self.chosen,True);p['entries']=[r for r in p['entries'] if r['state']=='stale']
            if not p['entries']:return
            if confirm(d,'明确导出旧结果','以下结果依据已变化；仅导出当时保存的数量，不更新或重新分配：\n'+'\n'.join(r['head']['number']+'：'+r['reason'] for r in p['entries'])):d.accept();self.begin(p)
        v.addWidget(button('导出勾选旧结果（需核对）',old_export))
        def cancel_pending():
            if not self.batch_id:QMessageBox.information(d,'请选择批次','返回结果页，先选择要撤销未完成任务的批次。');return
            reason,ok=QInputDialog.getText(d,'撤销本批未完成任务','只撤销尚未生成的任务，已生成单保留；填写原因：')
            if ok and reason.strip() and confirm(d,'撤销本批未完成任务','确认结束本批尚未生成的任务？'):
                try:run(d,batches.cancel_unfinished,(self.path,self.batch_id,reason),'撤销未完成任务');d.accept()
                except Exception as exc:self.status.setText(str(exc))
        v.addWidget(button('撤销本批未完成任务',cancel_pending))
        def logs():
            log=jst_ui.Logs(d,self.host,self.day.text(),self.day.text());log.exec();log.deleteLater()
        v.addWidget(button('批次作业日志',logs));v.addWidget(button('返回结果页',d.reject));d.exec();d.deleteLater();self.refresh()

    def one_click(self):
        import jst_one_click as operation
        from catalog_session_ui import manager
        from interaction_ui import review_rows,show_rows
        from lifecycle_ui import warn
        from runtime_support import Cancelled
        if self.worker or self.reading or self.reviewing or self.closing or self.host.closing:return
        heads=[r['head'] for r in self.visible_records() if service.key(r['head']) in self.chosen]
        if not heads:return
        self.reviewing=True;self.controls()
        try:
            from platform_order_scope import choose
            heads = choose(self,self.path,heads)
            if not heads:return
            prepared=run(self,operation.preflight,(self.path,heads),'核对所选仓单与历史验证')
            if self.host.closing:return
            ready=prepared['ready']
            actions={'allocate':'刷新本仓库存后分配并导入','order':'按有效生成单导入','recover':'按已保存原单核对'}
            headers=('门店','仓','补货单号','聚水潭店铺','本次处理')
            rows=[(e['head'].get('name') or e['head']['store'], e['head']['warehouse'],e['head']['number'],
                   e['shop']['name'],actions[e['mode']]) for e in ready]
            rows.extend((e['head'].get('name') or e['head']['store'],e['head']['warehouse'],e['head']['number'],
                         '—',e['message']) for e in prepared['skipped'])
            if not ready:
                show_rows(self,'所选仓单待核',headers,rows)
                return
            note=(f'本次处理 {len(ready)} 张仓单。平台预览通过后按实际分配的正数量导入。'
                  '已发起导入的单沿原单号核对。付款资料回读后由业务人员确认实际付款。')
            if not review_rows(self,'确认一键分配下单',headers,rows,note,'确认本次作业'):return
            if self.host.closing:return
            def adopted(event):
                if event.get('kind')=='adopted' and event.get('catalog'):
                    self.host.current_warehouse_adopted(event['catalog'])
            result=run(self,operation.execute,(self.path,ready,manager(self.host).session),'聚水潭一键分配下单',
                       on_business_event=adopted)
            if self.host.closing:return
            runner=getattr(self.host,'jst_fulfillment_runner',None)
            if runner:runner.refresh_pending()
            show_rows(self,'一键作业结果',('门店','仓','补货单号','聚水潭内部单号','状态','处理提示'),[
                (r['head'].get('name') or r['head']['store'],r['head']['warehouse'],r['head']['number'],
                 r.get('internal') or '—',r['status'],r['message']) for r in [*result,*prepared['skipped']]])
        except Cancelled:
            self.status.setText('已停止后续作业；已保存分配和原下单任务可继续核对。')
        except Exception as exc:
            warn(self,exc)
        finally:
            self.reviewing=False
            self.controls()
            if not self.host.closing:self.refresh()

    def auto_prepare(self):
        import jst_auto_order
        from jst_auto_order_ui import notify,open_orders
        if self.worker or self.reviewing:return
        self.reviewing=True;self.controls()
        def prepare():
            selected=jst_auto_order.selection(self.visible_records(),self.chosen)
            from platform_order_scope import choose
            selected=choose(self,self.path,selected,lambda row:row['head'])
            if not selected:return
            result=run(self,jst_auto_order.prepare,(self.path,selected),'按勾选仓单制表')
            if not self.closing and not self.host.closing:
                if result['ready']:self.focus_redo(open_orders(self,self.host,result['ready'],result['failed']))
                elif result['failed']:
                    from jst_order_dialogs import failure_rows,show_job_details
                    show_job_details(self,failure_rows(result['failed']))
        try:notify(self,prepare)
        finally:self.reviewing=False;self.refresh()

    def auto_tasks(self):
        from jst_auto_order_ui import notify,open_orders
        if self.worker or self.reviewing:return
        self.reviewing=True;self.controls()
        try:self.focus_redo(notify(self,lambda:open_orders(self,self.host)))
        finally:self.reviewing=False;self.refresh()

    def focus_redo(self,head):
        if not head:return
        self.batch_id=None
        self.scope=[head]
        self.scope_btn.show()
        self.redo_target=service.key(head)
        self.chosen={self.redo_target}
        day=QDate.fromString(head['day'],'yyyy-MM-dd')
        with QSignalBlocker(self.day),QSignalBlocker(self.end),QSignalBlocker(self.search),QSignalBlocker(self.warehouse):
            self.day.setDate(day)
            self.end.setDate(day)
            self.search.clear()
            self.warehouse.setCurrentText('全部仓')

    def auto_profiles(self):
        from jst_auto_order_ui import notify,profiles
        if self.worker or self.reviewing:return
        heads=[r['head'] for r in self.visible_records() if service.key(r['head']) in self.chosen]
        self.reviewing=True;self.controls()
        try:notify(self,lambda:profiles(self,self.host,heads))
        finally:self.reviewing=False;self.refresh()

    def shutdown(self):self.closing=True;self.timer.stop();self.reader.shutdown()
