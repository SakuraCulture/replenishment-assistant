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
    def __init__(self,host,scope=None,parent=None):
        super().__init__(parent);self.host=host;self.path=host.work_repository.path;self.scope=scope
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
        self.reallocate_btn=button('重新分配本批勾选单',self.reallocate);row.addWidget(self.reallocate_btn);v.addLayout(row)
        self.total=SummaryLabel('正在读取已保存结果…');v.addWidget(self.total)
        self.counts=label('','muted');v.addWidget(self.counts)
        row=QHBoxLayout();self.search=QLineEdit();self.search.setPlaceholderText('搜索门店 / 仓 / 补货单号');self.search.setClearButtonEnabled(True);row.addWidget(self.search,1)
        self.warehouse=QComboBox();self.warehouse.addItems(['全部仓','河北仓','山东仓']);row.addWidget(self.warehouse)
        self.lock_filter=QComboBox();self.lock_filter.addItems(['全部锁状态','已锁','未锁']);row.addWidget(self.lock_filter)
        row.addWidget(button('勾选筛选结果',self.select_visible));row.addWidget(button('清空勾选',self.clear_selection));v.addLayout(row)
        v.addWidget(label('展开门店可单选仓单；双击查看商品。导出和新建批次只处理当前筛选中的订单，数量在按钮上显示。','muted'))
        self.table=QTreeWidget();self.table.setColumnCount(8);self.table.setHeaderLabels(['门店 / 仓单','准备情况','需求金额','已分配金额','减少金额','减量SKU','最近导出','下单锁定'])
        self.table.setAlternatingRowColors(True);self.table.setUniformRowHeights(True);self.table.setSortingEnabled(False)
        for i,width in enumerate((320,200,110,110,110,85,160,120)):self.table.setColumnWidth(i,width)
        self.table.itemChanged.connect(self.checked);self.table.itemDoubleClicked.connect(self.detail);v.addWidget(self.table,1)
        self.selection=label('已勾选0单，可导出0份','muted');v.addWidget(self.selection)
        folder=QHBoxLayout();folder.addWidget(label('导出位置'));self.folder=QLineEdit(export_locations.initial_directory(self));self.folder.setReadOnly(True);self.folder.setAccessibleName('本次导出文件夹');folder.addWidget(self.folder,1)
        self.folder_btn=button('更改本次目录',self.choose_folder);folder.addWidget(self.folder_btn);v.addLayout(folder)
        self.progress=QProgressBar();self.progress.setRange(0,1);v.addWidget(self.progress)
        self.status=label('正在读取结果…','muted');v.addWidget(self.status)
        row=QHBoxLayout();self.all_btn=button('导出本批全部（0份）',self.export_all,True);self.selected_btn=button('导出勾选（0份）',self.export_selected)
        self.open_btn=button('打开导出文件夹',self.open_folder);row.addWidget(self.all_btn);row.addWidget(self.selected_btn);row.addWidget(self.open_btn);v.addLayout(row)
        row=QHBoxLayout();self.payment_all_btn=button('按当前筛选核对付款（0单）',self.payment_all)
        self.payment_selected_btn=button('按勾选核对付款（0单）',self.payment_selected)
        row.addWidget(self.payment_all_btn);row.addWidget(self.payment_selected_btn);v.addLayout(row)
        row=QHBoxLayout();self.retry_btn=button('重试未完成导出',self.retry);self.stop_btn=button('停止本次导出',self.stop)
        row.addWidget(self.retry_btn);row.addWidget(self.stop_btn);self.tools_btn=button('其他订单操作',self.tools);row.addWidget(self.tools_btn);v.addLayout(row)
        self.day.dateChanged.connect(self.day_changed);self.end.dateChanged.connect(self.day_changed);self.lock_filter.currentIndexChanged.connect(self.filter_changed);self.search.textChanged.connect(self.filter_changed);self.warehouse.currentIndexChanged.connect(self.filter_changed)
        self.timer=QTimer(self);self.timer.setInterval(3000);self.timer.timeout.connect(self.poll);self.timer.start()
        self.controls();QTimer.singleShot(0,self.refresh)

    def controls(self):
        busy=self.worker is not None or self.reviewing;ready=self.data is not None and not self.closing
        n=len(service.export_plan(self.data,{service.key(r['head']) for r in self.visible_records()})['entries']) if self.data else 0
        m=len(service.export_plan(self.data,self.chosen)['entries']) if self.data else 0
        self.all_btn.setText(f'导出当前筛选（{n}份）');self.selected_btn.setText(f'导出勾选（{m}份）')
        self.all_btn.setEnabled(ready and not busy and n>0);self.selected_btn.setEnabled(ready and not busy and m>0)
        self.payment_all_btn.setText(f'按当前筛选核对付款（{len(self.visible_records())}单）');self.payment_selected_btn.setText(f'按勾选核对付款（{len(self.chosen)}单）')
        self.payment_all_btn.setEnabled(ready and not busy and bool(self.visible_records()));self.payment_selected_btn.setEnabled(ready and not busy and bool(self.chosen))
        for b in (self.payment_all_btn,self.payment_selected_btn):b.setToolTip('直接使用已保存生成单，无需导出再导入；核对实际已付款后才保存。')
        self.selection.setText(f'已勾选 {len(self.chosen)} 单，可导出 {m} 份；已导出、锁单、付款和收货不会移除已有结果。')
        for b in (self.day,self.end,self.refresh_btn,self.scope_btn,self.folder_btn,self.prepare_btn,self.tools_btn,self.batch_btn,self.all_batches_btn):b.setEnabled(not busy and not self.closing)
        self.day.setEnabled(not busy and not self.batch_id);self.end.setEnabled(not busy and not self.batch_id)
        correction_rows=[r for r in self.records if service.key(r['head']) in self.chosen and r.get('batch_id') and (r.get('gid') or r.get('original_zero'))]
        self.continue_btn.setEnabled(ready and not busy and bool(self.batch_id) and bool(self.data.get('batch_pending')))
        self.reallocate_btn.setText(f'按原批重新分配勾选单（{len(correction_rows)}单）')
        self.reallocate_btn.setEnabled(ready and not busy and bool(correction_rows))
        self.reallocate_btn.setToolTip('沿用所选订单的原批库存，只替换所选未锁、未收货订单；旧付款依据因新回传失效时，保留旧付款并在分配后重新核对。多个原批分别处理。')
        self.continue_btn.setToolTip('先选择分配批次；继续使用其冻结库存和原顺序')
        if not n:self.all_btn.setToolTip('当前筛选没有有效生成单；请准备批次或核对待处理原因')
        self.stop_btn.setEnabled(self.worker is not None and not self.worker.cancel.is_set());self.retry_btn.setEnabled(bool(self.remaining) and not busy and not self.closing)

    def refresh(self):
        if self.closing or self.worker or self.reviewing:return
        self.reading=True;self.controls()
        self.scope_note.setText(('分配批次 '+self.batch_id[:8]+' · 冻结库存、原分配顺序' if self.batch_id else self.day.text()+' 至 '+self.end.text()+' · 日期范围（可包含多个分配批次）')+(' · 限定所选 '+str(len(self.scope))+' 单' if self.scope is not None else ''))
        if self.data is None:self.progress.show();self.progress.setRange(0,0)
        self.reader.request({'kind':'jst_results','day':self.day.text(),'date_end':self.end.text(),'batch_id':self.batch_id,'scope':[list(service.key(h)) for h in self.scope] if self.scope is not None else None},self.ready,self.failed_read,self.read_progress)
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
        terms=self.search.text().lower().split();wh=self.warehouse.currentText() if self.warehouse.currentIndex() else ''
        lock=self.lock_filter.currentText()
        return [r for r in self.records if (not wh or r['head']['warehouse']==wh) and (lock=='全部锁状态' or bool(r['locked'])==(lock=='已锁')) and all(t in ' '.join(str(r['head'][k]) for k in ('name','store','warehouse','number')).lower() for t in terms)]

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
            top=QTreeWidgetItem([g['name']+'（'+g['store']+'）',f'{n}/{len(rs)} 仓单可导出',*[review.amount_text(m,k) if any(r['projection'] and r['state'] in ('ready','zero') for r in rs) else '待处理' for k in ('demand','allocated','reduced')],str(m['reduced_sku']),max((r['last_export'] for r in rs if r['last_export'] not in ('—','未导出')),default='未导出'),f"已锁 {sum(r['locked'] for r in rs)}/{len(rs)}"])
            self.table.addTopLevelItem(top);top.setData(0,ROLE,g['store']);top.setFlags(top.flags()|Qt.ItemFlag.ItemIsAutoTristate|Qt.ItemFlag.ItemIsUserCheckable)
            for r in rs:
                m=service.aggregate([r]);child=QTreeWidgetItem([r['head']['warehouse']+' · '+r['head']['number'],r['reason'],*[review.amount_text(m,k) if r['projection'] else '待处理' for k in ('demand','allocated','reduced')],str(m['reduced_sku']) if r['projection'] else '—',r['last_export'],'已锁定' if r['locked'] else '未锁'])
                top.addChild(child);child.setData(0,ROLE,list(service.key(r['head'])));child.setFlags(child.flags()|Qt.ItemFlag.ItemIsUserCheckable);child.setCheckState(0,CHECKED if service.key(r['head']) in self.chosen else Qt.CheckState.Unchecked)
                for c in range(8):child.setToolTip(c,child.text(c))
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
            v.addWidget(button('返回结果页',d.accept));d.exec();d.deleteLater()
        except Exception as exc:self.status.setText(str(exc))

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
            if receipt is not None:self.payment_notice=f'付款核对完成 {len(receipt)} 单，新增/更新 {sum(r["changed"] for r in receipt)} 单；生成、导出和锁单记录保留。'
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
            QMessageBox.information(self,'没有可加入新批的订单','当前筛选没有从未分配的订单。有效生成单可直接导出；回传已变化的旧分配不能直接导出，请勾选后点击“按原批重新分配勾选单”。已锁单或已有收货记录的订单须先核对外部执行事实。');return
        jst_ui.generate(self,self.host,[r['head'] for r in rows]);self.refresh()
    def tools(self):
        if not self.data or self.worker:return
        import jst_ui
        heads=[r['head'] for r in self.records if service.key(r['head']) in self.chosen]
        d=QDialog(self);d.setWindowTitle('其他订单操作');v=QVBoxLayout(d);v.addWidget(label(f'已勾选 {len(heads)} 张仓单；锁单不等于付款。'))
        def action(fn):
            if heads:fn(d,self.host,heads);d.accept()
            else:QMessageBox.information(d,'请先勾选','返回结果页勾选要操作的门店或仓单。')
        for title,fn in [('确认已下单并锁定',jst_ui.change_lock),('更正已锁订单',lambda p,h,s:jst_ui.change_lock(p,h,s,True)),('撤销未下单生成单',jst_ui.cancel_orders),('查看当时资料依据',jst_ui.detail)]:v.addWidget(button(title,lambda checked=False,f=fn:action(f)))
        def old_export():
            p=service.export_plan(self.data,self.chosen,True);p['entries']=[r for r in p['entries'] if r['state']=='stale']
            if not p['entries']:return
            if confirm(d,'明确导出旧结果','以下结果依据已变化；仅导出当时保存的数量，不更新或重新分配：\n'+'\n'.join(r['head']['number']+'：'+r['reason'] for r in p['entries'])):d.accept();self.begin(p)
        v.addWidget(button('导出勾选旧结果（需核对）',old_export))
        def cancel_pending():
            if not self.batch_id:QMessageBox.information(d,'请选择批次','返回结果页，先选择要撤销未完成任务的批次。');return
            reason,ok=QInputDialog.getText(d,'撤销本批未完成任务','只撤销尚未生成的任务，已生成单保留；填写原因：')
            if ok and reason.strip() and confirm(d,'撤销本批未完成任务','撤销后不会自动恢复；已生成单和已锁单保留。'):
                try:run(d,batches.cancel_unfinished,(self.path,self.batch_id,reason),'撤销未完成任务');d.accept()
                except Exception as exc:self.status.setText(str(exc))
        v.addWidget(button('撤销本批未完成任务',cancel_pending))
        def logs():
            log=jst_ui.Logs(d,self.host,self.day.text(),self.day.text());log.exec();log.deleteLater()
        v.addWidget(button('批次作业日志',logs));v.addWidget(button('返回结果页',d.reject));d.exec();d.deleteLater();self.refresh()
    def shutdown(self):self.closing=True;self.timer.stop();self.reader.shutdown()
