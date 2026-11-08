"""One final list: exact pack editor, list fill, measured preview, scoped approval."""
import copy
from decimal import Decimal
from PySide6.QtCore import Qt,QAbstractTableModel,QModelIndex,QPersistentModelIndex,QSortFilterProxyModel,QTimer
from PySide6.QtGui import QValidator,QColor
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QTableView,QLineEdit,QComboBox,QCheckBox,
    QDialog,QMessageBox,QAbstractSpinBox,QStyledItemDelegate,QTableWidget,QTableWidgetItem,QSplitter,QLabel,QScrollArea)
from ui_controls import button,label
from stocking_ui import AsyncDialog,StockingDialog
from runtime_support import checkpoint
from export_progress import RowProgress,prefixed
import preissue as rules
import result_semantics as sem
import work_repository as wr
from draft_review import DraftReview
import review_filters as filters
import new_product_focus
from ui_review_filters import ReviewFilters
import final_list_filters as business_filters

HEADERS=['选择','门店','归属仓','商品编码','商品名称','可用','在途','箱规 / 单位','自动安排','拟签发数量','差额','拟签发金额','补后持有','调整原因','校验 / 风险','预测持有天数','清单目标','超过目标数量']


class PackInput(QAbstractSpinBox):
    """No implicit fixup: keyboard/paste and arrows share the exact pack contract."""
    def __init__(self,step,parent=None):
        super().__init__(parent);self.step=rules.decimal(step);self.setKeyboardTracking(False);self.setValue(0)
        self.setToolTip(f'每次{self.step:g}库存单位；只允许0、{self.step:g}、{2*self.step:g}……')
    def setValue(self,value):self.lineEdit().setText(str(value))
    def value(self):return rules.quantity(self.lineEdit().text(),self.step)
    def validate(self,text,pos):
        try:rules.quantity(text,self.step);state=QValidator.State.Acceptable
        except ValueError:state=QValidator.State.Intermediate
        return state,text,pos
    def fixup(self,text):return text
    def stepBy(self,steps):
        try:self.setValue(rules.scalar(max(Decimal(0),rules.decimal(self.value())+self.step*steps)))
        except ValueError:pass
    def stepEnabled(self):
        result=QAbstractSpinBox.StepEnabledFlag.StepUpEnabled
        try:
            if self.value()>0:result|=QAbstractSpinBox.StepEnabledFlag.StepDownEnabled
        except ValueError:pass
        return result


class PackDelegate(QStyledItemDelegate):
    def createEditor(self,parent,option,index):
        source=index.model().mapToSource(index);model=index.model().sourceModel();editor=PackInput(model.entries[source.row()][1]['BD'],parent)
        editor.source_index=QPersistentModelIndex(source);return editor
    def setEditorData(self,editor,index):editor.setValue(index.data(Qt.ItemDataRole.EditRole))
    def setModelData(self,editor,proxy,index):
        # The editor owns a source identity, never a row number in a changing proxy.
        source=editor.source_index
        if source.isValid():source.model().setData(QModelIndex(source),editor.lineEdit().text(),Qt.ItemDataRole.EditRole)


class FinalModel(QAbstractTableModel):
    def __init__(self,results,drafts,changed,parent=None):
        super().__init__(parent);self.results=results;self.entries=[(s,d) for s,r in results.items() for d in r['rows']]
        self.edits={s:copy.deepcopy(drafts.get(s,{}).get('edits',rules.requests(r))) for s,r in results.items()}
        self.lists={s:copy.deepcopy(drafts.get(s,{}).get('lists',r.get('preissue',{}).get('lists',{}))) for s,r in results.items()}
        self.checked=set();self.invalid={};self.risks={};self.confirmed=set();self.reviewed=False;self.changed=changed;self.reason='';self.sequence=1+max((e.get('sequence',0) for v in self.edits.values() for e in v.values()),default=0)
        self.searches=[filters.search_text(d,s+' '+results[s]['store_name']) for s,d in self.entries]
        self.blocks=[rules.blocked(d) for s,d in self.entries]
        self.positions={self.key(i):i for i in range(len(self.entries))}
        self.money=DraftReview(results,self.edits,self.lists)
        self.changed_keys=set();self.all_changed=False
    def key(self,i):s,d=self.entries[i];return s,d['E']
    def qty(self,i):
        s,d=self.entries[i];return self.edits[s].get(d['E'],{}).get('quantity',self.automatic(i))
    def automatic(self,i):
        _,d=self.entries[i];return d.get('_preissue',{}).get('automatic_quantity',sem.order_quantity(d))
    def rowCount(self,parent=QModelIndex()):return 0 if parent.isValid() else len(self.entries)
    def columnCount(self,parent=QModelIndex()):return 0 if parent.isValid() else len(HEADERS)
    def headerData(self,n,orientation,role=Qt.ItemDataRole.DisplayRole):
        if role==Qt.ItemDataRole.DisplayRole:return HEADERS[n] if orientation==Qt.Orientation.Horizontal else n+1
    def flags(self,index):
        if not index.isValid():return Qt.ItemFlag.NoItemFlags
        flags=Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsSelectable
        if index.column()==0:flags|=Qt.ItemFlag.ItemIsUserCheckable
        if index.column() in (9,13) and not self.blocks[index.row()]:flags|=Qt.ItemFlag.ItemIsEditable
        return flags
    def data(self,index,role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():return None
        i=index.row();col=index.column();s,d=self.entries[i];key=(s,d['E']);e=self.edits[s].get(d['E'],{});q=self.qty(i);a=self.automatic(i)
        if col==0:
            if role==Qt.ItemDataRole.CheckStateRole:return Qt.CheckState.Checked if key in self.checked else Qt.CheckState.Unchecked
            return None
        if role==Qt.ItemDataRole.ForegroundRole and (key in self.invalid or col==14 and self.blocks[i]):return QColor('#B23424')
        if role==Qt.ItemDataRole.BackgroundRole and d['E'] in self.edits[s]:return QColor('#EDF5EF')
        if role not in (Qt.ItemDataRole.DisplayRole,Qt.ItemDataRole.ToolTipRole,Qt.ItemDataRole.EditRole,Qt.ItemDataRole.UserRole):return None
        money=float(rules.decimal(q)*rules.decimal(d['V'])) if sem.numeric(q) and sem.numeric(d.get('V')) else None
        held=float(rules.holding(d))+q if not self.blocks[i] and sem.numeric(q) else None
        risk=('已确认：' if key in self.confirmed else '待确认：')+self.risks[key] if key in self.risks else ''
        note=self.invalid.get(key) or self.blocks[i] or risk or ('请填写调整原因' if e and not e.get('reason') else '')
        rate=d.get('AN',0)*d.get('AM',0) if all(sem.numeric(d.get(k)) for k in ('AN','AM')) else 0
        target=e.get('target') if e.get('strategy')=='target' else None
        values={1:self.results[s]['store_name'],2:d.get('AU'),3:d['E'],4:d.get('D'),5:d.get('K'),6:d.get('L'),
                7:f'{sem.fmt(d.get("BD"))} {d.get("G","")}',8:a,9:q,10:q-a if sem.numeric(q) and sem.numeric(a) else None,11:money,12:held,13:e.get('reason',''),14:note,
                15:held/rate if held is not None and rate>0 else None,16:target,17:max(0,held-target) if held is not None and sem.numeric(target) else None}
        value=values[col]
        if role in (Qt.ItemDataRole.EditRole,Qt.ItemDataRole.UserRole):return value
        return sem.fmt(value)
    def setData(self,index,value,role=Qt.ItemDataRole.EditRole):
        if not index.isValid():return False
        i=index.row();s,d=self.entries[i];key=(s,d['E'])
        if index.column()==0 and role==Qt.ItemDataRole.CheckStateRole:
            if value in (Qt.CheckState.Checked,Qt.CheckState.Checked.value):self.checked.add(key)
            else:self.checked.discard(key)
            self.changed_keys.add(key);self.dataChanged.emit(index,index);self.changed(False);return True
        if self.blocks[i] or index.column() not in (9,13):return False
        try:
            q=rules.quantity(value,d['BD']) if index.column()==9 else self.qty(i)
            from restock_policy import guard_new
            guard_new(self.results[s],d,'manual',q)
            ref=self.edits[s].get(d['E'],{}).get('list_revision')
            if self.lists[s].get(ref,{}).get('kind')=='new':guard_new(self.results[s],d,'new',q)
        except ValueError as exc:
            self.invalid[key]=str(exc);self.dataChanged.emit(self.index(i,9),self.index(i,14));self.changed(True);return False
        prev=self.edits[s].get(d['E'],{});reason=str(value).strip() if index.column()==13 else prev.get('reason') or self.reason
        self.edits[s][d['E']]={**prev,'quantity':q,'kind':'manual','reason':reason,'warehouse':d['AU'],'sequence':self.sequence,'edited_at':wr.now()}
        self.sequence+=1;self.invalid.pop(key,None);self.money.update(key);self.changed_keys.add(key);self.dataChanged.emit(self.index(i,9),self.index(i,17));self.changed(True);return True
    def replace_edits(self,updates,lists=None,cleared=()):
        self.beginResetModel()
        for s,e in updates.items():self.edits[s]=copy.deepcopy(e)
        if lists:
            for s,l in lists.items():self.lists[s]=copy.deepcopy(l)
        for key in cleared:self.invalid.pop(key,None)
        self.risks.clear();self.money=DraftReview(self.results,self.edits,self.lists);self.all_changed=True;self.endResetModel();self.changed(True)
    def notify(self,first=0,last=None):
        if self.entries:self.dataChanged.emit(self.index(0,first),self.index(len(self.entries)-1,self.columnCount()-1 if last is None else last))


class FinalFilter(QSortFilterProxyModel):
    def __init__(self,parent):
        super().__init__(parent);self.needle='';self.store='';self.warehouse='';self.mode='全部';self.facets={};self.business={};self.focused=set();self.focus_known=False
        # Source data may change inside delegate commit. Rebuild only after the
        # editor has closed, using Qt's full invalidation protocol.
        self.setDynamicSortFilter(False)
    def filterAcceptsRow(self,i,parent,ignore_facets=False):
        m=self.sourceModel();s,d=m.entries[i]
        if any(word not in m.searches[i] for word in self.needle.split()) or self.store and s!=self.store or self.warehouse and d.get('AU')!=self.warehouse:return False
        q=m.qty(i);key=(s,d['E'])
        if not business_filters.accepts(d,q,self.business):return False
        if not ignore_facets and not filters.accepts(m.money.views[key],self.facets,self.focused,self.focus_known):return False
        return (self.mode=='全部' or self.mode=='可调整' and not m.blocks[i] or self.mode=='已修改' and d['E'] in m.edits[s] or self.mode=='拟签发大于0' and sem.numeric(q) and q>0 or
                self.mode=='拟签发为0' and q==0 or self.mode=='资料待核' and bool(m.blocks[i]) and sem.work_eligible(d) and d.get('_source')!='地采商品' or self.mode=='不可安排' and (bool(sem.exclusion_reason(d)) or d.get('_source')=='地采商品') or
                self.mode=='未核对修改' and not m.reviewed and d['E'] in m.edits[s] or self.mode=='风险已确认' and key in m.confirmed or
                self.mode=='风险待确认' and key in m.risks and key not in m.confirmed or self.mode=='已勾选' and key in m.checked)


def preview_batch(results,edits,lists,day,progress=lambda _:None,cancel=None):
    out={};count=len(results)
    for i,(s,r) in enumerate(results.items(),1):
        checkpoint(cancel);progress(RowProgress(f'核对门店 {i}/{count}：{r["store_name"]}',i-1,count))
        out[s]=rules.preview(r,edits[s],day,lists=lists[s],cancel=cancel,progress=prefixed(progress,f'{i}/{count}店'))
    progress(RowProgress('全部门店核对完成',count,count));return out


def finalize_batch(prepared,confirmations,results,progress=lambda _:None,cancel=None):
    out={}
    for i,(s,p) in enumerate(prepared.items(),1):
        checkpoint(cancel);progress(RowProgress(f'应用前核对 {i}/{len(prepared)}',i-1,len(prepared)))
        out[s]=rules.finalize(p,confirmations,current=results[s]);rules.validate_result(out[s],p['result'].get('preissue',{}).get('day',''),cancel=cancel)
    checkpoint(cancel);return out


def fill_batch(results,edits,lists,listing,replace,sequence,progress=lambda _:None,cancel=None):
    out={};pending=[];skipped=[];new_lists=copy.deepcopy(lists);defaults=listing['defaults']
    for i,(s,r) in enumerate(results.items(),1):
        checkpoint(cancel);progress(RowProgress(f'清单补足 {i}/{len(results)}：{r["store_name"]}',i-1,len(results)))
        plan=rules.list_edits(r,listing,defaults['strategy'],defaults.get('target'),defaults['scopes'],replace_manual=replace,sequence=sequence,edits=edits[s])
        out[s]=plan['edits'];new_lists[s].update(plan['lists'])
        pending.extend({'store':s,**x} for x in plan['pending']);skipped.extend({'store':s,**x} for x in plan['skipped'])
    return {'edits':out,'lists':new_lists,'pending':pending,'skipped':skipped}


class FinalListDialog(AsyncDialog):
    def __init__(self,host,codes,listing=None):
        super().__init__(host);self.host=host;self.codes=list(codes);self.results={s:host.results[s] for s in codes};self.prepared=None;self.outcome=None;self.dirty=False
        from normal_delivery import job_head
        self.job_head=job_head(host.work_repository,host.job_day)
        self.setObjectName('finalListDialog');self.setWindowTitle('最终清单 · 直接改量 / 按清单补足');self.resize(1450,850)
        v=QVBoxLayout(self);self.body=QWidget();v.addWidget(self.body,1);b=QVBoxLayout(self.body)
        b.addWidget(label('最终清单：原自动安排留作对照，修改只影响所选商品；确认后按此数量签发。','section'))
        outer=b;controls=QWidget();b=QVBoxLayout(controls);b.setContentsMargins(0,0,0,0)
        control_scroll=QScrollArea();control_scroll.setWidgetResizable(True);control_scroll.setWidget(controls);control_scroll.setMaximumHeight(210)
        toggle=QCheckBox('展开筛选与批量操作（收起后筛选和勾选继续有效）');toggle.toggled.connect(control_scroll.setVisible);control_scroll.hide()
        outer.addWidget(toggle);outer.addWidget(control_scroll)
        numeric_row=QHBoxLayout();self.business_btn=button('类目 / 销量 / 库存筛选',self.edit_business_filters);numeric_row.addWidget(self.business_btn)
        self.business_summary=label(business_filters.description({}),'muted');self.business_summary.setWordWrap(True);numeric_row.addWidget(self.business_summary,1);outer.addLayout(numeric_row)
        top=QHBoxLayout();self.search=QLineEdit();self.search.setPlaceholderText('商品名称 / 编码 / 条码 / 门店；空格分词');self.search.setClearButtonEnabled(True);top.addWidget(self.search,1)
        self.stores=QComboBox();self.stores.addItem('全部所选门店','')
        for s in codes:self.stores.addItem(self.results[s]['store_name'],s)
        top.addWidget(self.stores);self.warehouses=QComboBox();self.warehouses.addItem('全部仓','')
        for wh in dict.fromkeys(d.get('AU','') for r in self.results.values() for d in r['rows']):self.warehouses.addItem(wh,wh)
        top.addWidget(self.warehouses);self.scope=QComboBox();self.scope.addItems(['可调整','全部','已修改','拟签发大于0','拟签发为0','资料待核','不可安排','未核对修改','风险待确认','风险已确认','已勾选']);self.scope.setToolTip('查看范围；可以与商品分组等条件组合');top.addWidget(self.scope);top.addWidget(button('重置全部筛选',self.reset_filters));b.addLayout(top)
        self.facets=ReviewFilters();b.addWidget(self.facets)
        actions=QHBoxLayout();actions.addWidget(button('按清单补足',self.choose_list));actions.addWidget(button('勾选当前筛选',lambda:self.check_visible(True)));actions.addWidget(button('清空全部勾选',self.clear_checks));self.bulk_btn=button('批量设数量',self.bulk);actions.addWidget(self.bulk_btn);self.restore_btn=button('恢复所选自动数量',self.restore);actions.addWidget(self.restore_btn);actions.addWidget(button('放弃所选门店全部调整',self.restore_all));b.addLayout(actions)
        selection=QHBoxLayout();selection.addWidget(label('批量操作范围'));self.selection_scope=QComboBox();self.selection_scope.addItems(['当前可见勾选','全部勾选（包含筛选外）']);selection.addWidget(self.selection_scope);selection_hint=label('直接编辑单行无需勾选；切换筛选保留勾选。','muted');selection_hint.setWordWrap(False);selection.addWidget(selection_hint);selection.addStretch();b.addLayout(selection)
        reasons=QHBoxLayout();reasons.addWidget(label('修改原因'));self.reason=QComboBox();self.reason.addItems(['请选择原因','活动备货','陈列需要','经营判断','暂不补货','其他']);reasons.addWidget(self.reason);self.reason_text=QLineEdit();self.reason_text.setPlaceholderText('其他原因必填；也可逐行编辑原因');reasons.addWidget(self.reason_text,1);reasons.addWidget(button('填入所选已修改行',self.assign_reason));b.addLayout(reasons)
        b=outer
        self.model=FinalModel(self.results,getattr(host,'preissue_drafts',{}),self.changed,self);self.proxy=FinalFilter(self);self.proxy.setSourceModel(self.model);self.proxy.setSortRole(Qt.ItemDataRole.UserRole)
        self.proxy.focused=new_product_focus.members(getattr(host,'new_focus',None));self.proxy.focus_known=getattr(host,'new_focus',None) is not None
        self.money_table=QTableWidget(0,7);self.money_table.setHorizontalHeaderLabels(['门店 · 未应用预览','拟签发金额','人工改量金额','较自动增减','新品金额','新品较自动增减','预算剩余 / 超出'])
        self.money_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);self.money_table.verticalHeader().hide();self.money_table.setAlternatingRowColors(True)
        for j,width in enumerate([190,145,145,145,145,155,220]):self.money_table.setColumnWidth(j,width)
        self.money_table.setToolTip('整店金额，不随商品／仓／搜索筛选改变。人工与新品可交叉，不相加。应用前为有效输入预览；缺价单列待核。');b.insertWidget(1,self.money_table)
        self.refresh_timer=QTimer(self);self.refresh_timer.setSingleShot(True);self.refresh_timer.timeout.connect(self.refresh_after_edit)
        self.filter_counter=None;self.proxy_dirty=False
        self.table=QTableView();self.table.setModel(self.proxy);self.table.setSortingEnabled(True);self.table.sortByColumn(-1,Qt.SortOrder.AscendingOrder);self.table.setItemDelegateForColumn(9,PackDelegate(self.table));self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableView.EditTrigger.DoubleClicked|QTableView.EditTrigger.EditKeyPressed)
        self.reading_identity=label('选择商品行后，这里常驻显示门店、仓及商品身份；横向滚动不丢失定位。','muted');self.reading_identity.setWordWrap(True);b.addWidget(self.reading_identity)
        def identity_changed(index,*_):
            values=[str(self.proxy.data(self.proxy.index(index.row(),c)) or '') for c in (1,2,3,4)] if index.isValid() else []
            self.reading_identity.setText('当前商品：'+' · '.join(values) if values else '请选择商品行；身份随当前行更新。')
        self.table.selectionModel().currentChanged.connect(identity_changed)
        self.proxy.modelReset.connect(lambda:identity_changed(QModelIndex()))
        self.splitter=QSplitter(Qt.Orientation.Vertical);self.splitter.addWidget(self.table);self.review=QTableWidget(0,4);self.review.setHorizontalHeaderLabels(['确认','门店 / 商品','约束类型','内容']);self.review.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);self.splitter.addWidget(self.review);self.splitter.setSizes([430,130]);b.addWidget(self.splitter,1)
        for i,width in enumerate([45,140,80,145,180,60,60,85,75,95,60,95,90,125,220]):self.table.setColumnWidth(i,width)
        self.review.setColumnWidth(0,48);self.review.setColumnWidth(1,220);self.review.setColumnWidth(2,100);self.review.setColumnWidth(3,850)
        self.review.hide()
        from table_readability import install,fit_window,full_row
        for table in (self.table,self.review,self.money_table):install(table)
        fit_window(self,1450,850)
        reading=QHBoxLayout();reading.addWidget(button('查看当前商品全部字段',lambda:full_row(self.table)));reading.addWidget(button('查看选中风险全文',lambda:full_row(self.review)))
        self.column_view=QComboBox();self.column_view.addItems(['全部字段','改量与金额','库存与覆盖','原因与风险']);reading.addWidget(self.column_view);reading.addStretch();b.addLayout(reading)
        def columns(index):
            visible=None if index==0 else ({0,1,2,3,4,7,8,9,10,11,13,14} if index==1 else {0,1,2,3,4,5,6,7,9,12,15,16,17} if index==2 else {0,1,2,3,4,9,10,13,14})
            for c in range(self.model.columnCount()):self.table.setColumnHidden(c,visible is not None and c not in visible)
        self.column_view.currentIndexChanged.connect(columns)
        self.metrics=label('','muted');b.addWidget(self.metrics)
        self.budget_note=label('','muted');b.addWidget(self.budget_note)
        bottom=QHBoxLayout();self.confirm_all=QCheckBox('已核对并确认本次列出的所有例外');bottom.addWidget(self.confirm_all);bottom.addStretch();self.preview_btn=button('核对修改与预算',self.preview,True);bottom.addWidget(self.preview_btn);self.apply_btn=button('应用最终数量',self.apply,True);self.apply_btn.setEnabled(False);bottom.addWidget(self.apply_btn);bottom.addWidget(button('保留为待核对草稿',self.keep_draft));b.addLayout(bottom)
        self.setup_async(v);v.addWidget(button('取消 / 放弃本次编辑',self.reject))
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.setInterval(250);self.timer.timeout.connect(self.filter);self.search.textChanged.connect(lambda:self.timer.start())
        for w in (self.stores,self.warehouses,self.scope):w.currentIndexChanged.connect(self.filter)
        self.reason.currentIndexChanged.connect(self.reason_changed);self.reason_text.textChanged.connect(self.reason_changed);self.confirm_all.toggled.connect(self.check_risks);self.review.itemChanged.connect(self.risk_checked)
        self.facets.changed.connect(self.filter);self.selection_scope.currentIndexChanged.connect(lambda *_:self.update_metrics());self.filter()
        if listing:QTimer.singleShot(0,lambda:self.use_list(listing))
    def reason_changed(self,*_):self.model.reason=self.reason_text.text().strip() if self.reason.currentText()=='其他' else self.reason.currentText() if self.reason.currentIndex()>0 else ''
    def filter(self,*_):
        self.timer.stop();self.proxy.needle=self.search.text().lower();self.proxy.store=self.stores.currentData();self.proxy.warehouse=self.warehouses.currentData();self.proxy.mode=self.scope.currentText();self.proxy.facets=self.facets.values()
        self.filter_counter=None;self.proxy_dirty=True
        if self.table.state()!=QTableView.State.EditingState:self.proxy.invalidate();self.proxy_dirty=False
        self.changed(False)
    def reset_filters(self):
        from PySide6.QtCore import QSignalBlocker
        locks=[QSignalBlocker(c) for c in (self.stores,self.warehouses,self.scope,self.search,self.facets)]
        self.search.clear();self.stores.setCurrentIndex(0);self.warehouses.setCurrentIndex(0);self.scope.setCurrentIndex(0);self.facets.reset();self.set_business_filters({});del locks;self.filter()
    def edit_business_filters(self):
        from final_list_filter_ui import FilterDialog
        dialog=FilterDialog([d for s,d in self.model.entries],self.proxy.business,self)
        if dialog.exec()==QDialog.DialogCode.Accepted:self.set_business_filters(dialog.spec)
        dialog.deleteLater()
    def set_business_filters(self,spec):
        business_filters.validate(spec);self.proxy.business=copy.deepcopy(spec)
        self.business_summary.setText(business_filters.description(spec));self.filter()
    def changed(self,edited=True):
        if edited:
            self.dirty=True;self.prepared=None;self.apply_btn.setEnabled(False);self.review.setRowCount(0);self.review.hide();self.confirm_all.setChecked(False)
            self.model.risks.clear();self.model.confirmed.clear();self.model.reviewed=False;self.model.notify(14,14)
            self.message.setText('有修改，待重新核对；当前金额为未应用预览。')
        if hasattr(self,'refresh_timer'):self.refresh_timer.start(0)
        if self.proxy.mode not in ('全部','可调整','资料待核','不可安排') or self.proxy.business or any(c.currentIndex()>0 for c in self.facets.fields.values()) or self.proxy.sortColumn()>=0 or self.model.all_changed:self.proxy_dirty=True
        if edited and self.proxy.business:self.filter_counter=None
        if self.proxy.mode in ('未核对修改','风险待确认','风险已确认','已勾选') or self.model.all_changed:self.filter_counter=None
        if edited:self.update_money()
        else:self.update_metrics()
    def refresh_after_edit(self):
        if self.table.state()==QTableView.State.EditingState:
            self.refresh_timer.start(50);return
        if self.proxy_dirty:self.proxy.invalidate();self.proxy_dirty=False
        self.update_metrics()
        if self.filter_counter is None:
            self.filter_counter=filters.FacetCounter(self.proxy.facets,self.proxy.focused,self.proxy.focus_known);keys=self.model.positions
        else:keys=self.model.changed_keys
        for key in keys:self.filter_counter.update(key,self.model.money.views[key],self.proxy.filterAcceptsRow(self.model.positions[key],QModelIndex(),True))
        self.model.changed_keys.clear();self.model.all_changed=False;self.facets.set_counts(self.filter_counter.counts)
    def update_metrics(self):
        shown={self.model.key(self.proxy.mapToSource(self.proxy.index(i,0)).row()) for i in range(self.proxy.rowCount())}
        count=sum(len(v) for v in self.model.edits.values())
        self.metrics.setText(f'所选{len(self.codes)}店 · 命中{len({s for s,k in shown})}店 / {self.proxy.rowCount()}款 · 已勾选{len(self.model.checked)}款（筛选外{len(self.model.checked-shown)}款） · 调整记录{count}条 · 非法输入{len(self.model.invalid)}行。数量按库存单位；每款遵循本仓箱规。')
        count=len(self.chosen());self.bulk_btn.setText(f'批量加减 / 设数量（{count}款）');self.restore_btn.setText(f'恢复自动数量（{count}款）');self.update_money()
    def update_money(self):
        codes=[self.stores.currentData()] if self.stores.currentData() else self.codes;self.money_table.setRowCount(len(codes));height=self.money_table.fontMetrics().height()+16;self.money_table.setFixedHeight(height+min(2,len(codes))*height)
        for i,s in enumerate(codes):
            r=self.results[s];a=self.model.money.summary(s);limit=r['budget'].get('limit') if r['budget'].get('enabled') else None
            def amt(key,unknown):return ('已计价 ' if a[unknown] else '')+f'{a[key]:,.2f}'+(f'（{a[unknown]}款待核）' if a[unknown] else '')
            values=[r['store_name'],amt('total','total_unknown'),amt('manual_amount','manual_unknown'),f"{a['manual_delta']:+,.2f}"+(f"（{a['delta_unknown']}款待核）" if a['delta_unknown'] else ''),amt('new_amount','new_unknown'),f"{a['new_delta']:+,.2f}"+(f"（{a['new_delta_unknown']}款待核）" if a['new_delta_unknown'] else ''),'不限预算' if limit is None else f"目标{limit:,.2f}；余{rules.decimal(limit)-a['total']:,.2f}"]
            for j,value in enumerate(values):
                if not self.money_table.item(i,j):self.money_table.setItem(i,j,QTableWidgetItem())
                self.money_table.item(i,j).setText(value)
        self.budget_note.setText('整店金额为未应用预览，不随商品筛选改变；人工改量与新品可能交叉，不能相加。签发前仍须核对预算和风险。')
    def check_visible(self,on):
        for i in range(self.proxy.rowCount()):self.model.checked.add(self.model.key(self.proxy.mapToSource(self.proxy.index(i,0)).row()))
        self.model.notify(0,0);self.changed(False)
    def clear_checks(self):self.model.checked.clear();self.model.notify(0,0);self.changed(False)
    def chosen(self):
        if not self.model.checked:return []
        visible={self.proxy.mapToSource(self.proxy.index(i,0)).row() for i in range(self.proxy.rowCount())} if self.selection_scope.currentIndex()==0 else None
        return [i for i in range(len(self.model.entries)) if self.model.key(i) in self.model.checked and (visible is None or i in visible)]
    def assign_reason(self):
        if not self.model.reason:self.message.setText('请先选择或填写原因');return
        for i in self.chosen():
            s,d=self.model.entries[i]
            if d['E'] in self.model.edits[s]:self.model.edits[s][d['E']].update(reason=self.model.reason,edited_at=wr.now());self.model.money.update((s,d['E']))
        self.model.notify(13,14);self.changed(True)
    def bulk(self):
        self.table.clearFocus();self.filter()
        indices=self.chosen()
        if not indices:self.message.setText('请先勾选需要修改的商品');return
        from final_list_bulk_ui import BulkDialog
        dialog=BulkDialog(self.model,indices,self.selection_scope.currentText(),self)
        if dialog.exec()==QDialog.DialogCode.Accepted and dialog.prepared is not None:
            self.model.sequence=dialog.prepared['sequence']
            self.model.replace_edits(dialog.prepared['edits'],cleared=[self.model.key(i) for i in indices])
        dialog.deleteLater()
    def restore(self):
        indices=self.chosen()
        if not indices:self.message.setText('请勾选要恢复的商品');return
        names='、'.join(dict.fromkeys(self.results[self.model.entries[i][0]]['store_name'] for i in indices))
        if QMessageBox.question(self,'恢复所选自动数量',self.selection_scope.currentText()+f'；门店：{names}\n恢复{len(indices)}行原自动数量并清除这些行的清单/人工覆盖？其余行保留。',QMessageBox.StandardButton.Ok|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)!=QMessageBox.StandardButton.Ok:return
        updates=copy.deepcopy(self.model.edits)
        for i in indices:s,row=self.model.entries[i];updates[s].pop(row['E'],None)
        self.model.replace_edits(updates,cleared=[self.model.key(i) for i in indices])
    def restore_all(self):
        if QMessageBox.question(self,'放弃所选门店全部调整',f'将清除本窗口{len(self.codes)}店的全部清单、人工和待核对调整（包括已不在结果中的商品），恢复自动数量。核对并应用后生效。',QMessageBox.StandardButton.Ok|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)!=QMessageBox.StandardButton.Ok:return
        self.model.replace_edits({s:{} for s in self.codes},{s:{} for s in self.codes},cleared=list(self.model.invalid))
    def preview(self):
        self.table.clearFocus()
        if self.model.invalid:self.message.setText('仍有非法数量，请修正或恢复对应行');return
        self.run(preview_batch,(self.results,copy.deepcopy(self.model.edits),copy.deepcopy(self.model.lists),self.host.job_day),self.preview_ready)
    def preview_ready(self,value):
        self.prepared=value;rows=[];self.model.risks={};self.model.confirmed.clear();self.model.reviewed=True;summary=[]
        for s,p in value.items():
            summary.append(f'{self.results[s]["store_name"]}：¥{self.results[s]["summary"]["amount"]:,.2f} → ¥{p["result"]["summary"]["amount"]:,.2f}')
            for risk in p['risks']:rows.append((s,risk));self.model.risks[(s,risk['sku'])]=risk['text']
            for error in p['errors']:rows.append((s,{'kind':'禁止签发','text':error}))
        self.review.blockSignals(True);self.review.setRowCount(len(rows))
        for i,(s,risk) in enumerate(rows):
            cell=QTableWidgetItem();cell.setFlags(Qt.ItemFlag.ItemIsEnabled|(Qt.ItemFlag.ItemIsUserCheckable if risk.get('id') else Qt.ItemFlag.NoItemFlags));cell.setData(Qt.ItemDataRole.UserRole,risk.get('id'));cell.setCheckState(Qt.CheckState.Unchecked);self.review.setItem(i,0,cell)
            for j,text in enumerate([self.results[s]['store_name']+' / '+risk.get('sku','整店'),{'zero':'人工不补','cap':'超库存额度','unknown':'无法评估','budget':'有限超支'}.get(risk['kind'],risk['kind']),risk['text']],1):self.review.setItem(i,j,QTableWidgetItem(text))
        self.review.blockSignals(False);self.review.setVisible(bool(rows));self.confirm_all.setChecked(False);self.model.notify(14,14);self.message.setText('；'.join(summary)+('；请确认列出的风险' if rows else '；已核对，无待确认风险'));self.risk_checked()
    def check_risks(self,on):
        self.review.blockSignals(True)
        for i in range(self.review.rowCount()):
            if self.review.item(i,0).data(Qt.ItemDataRole.UserRole):self.review.item(i,0).setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        self.review.blockSignals(False);self.risk_checked()
    def risk_checked(self,*_):
        allowed=bool(self.prepared) and not any(p['errors'] for p in self.prepared.values())
        allowed=allowed and all(not self.review.item(i,0).data(Qt.ItemDataRole.UserRole) or self.review.item(i,0).checkState()==Qt.CheckState.Checked for i in range(self.review.rowCount()))
        self.apply_btn.setEnabled(allowed)
        tokens={self.review.item(i,0).data(Qt.ItemDataRole.UserRole) for i in range(self.review.rowCount()) if self.review.item(i,0).checkState()==Qt.CheckState.Checked}
        self.model.confirmed={(s,r['sku']) for s,p in (self.prepared or {}).items() for r in p['risks'] if all(x['id'] in tokens for x in p['risks'] if x['sku']==r['sku'])}
        self.model.notify(14,14)
        self.proxy_dirty=True;self.filter_counter=None
        self.refresh_timer.start(0)
    def current_guard(self):
        from normal_delivery import job_head
        if self.host.readonly or self.host.input_dirty or self.host.job_day!=wr.today():raise ValueError('作业已失效或不是今日可编辑作业')
        if any(self.host.results.get(s) is not r or self.host.states[s]['state']!='已完成' for s,r in self.results.items()):raise ValueError('结果已变化，请重新打开最终清单')
        if job_head(self.host.work_repository,self.host.job_day)!=self.job_head:raise ValueError('当天草稿已被其他窗口更新，请重新读取核对')
    def apply(self):
        if not self.prepared:return
        try:self.current_guard()
        except Exception as exc:self.message.setText(str(exc));return
        tokens=[self.review.item(i,0).data(Qt.ItemDataRole.UserRole) for i in range(self.review.rowCount()) if self.review.item(i,0).checkState()==Qt.CheckState.Checked]
        self.run(finalize_batch,(self.prepared,tokens,self.results),self.applied)
    def applied(self,results):
        try:self.current_guard()
        except Exception as exc:self.message.setText(str(exc));return
        self.outcome={'action':'apply','results':results};self.dirty=False;self.accept()
    def keep_draft(self):
        if self.model.invalid:self.message.setText('非法数量不能保存；请修正或恢复对应行');return
        try:self.current_guard()
        except Exception as exc:self.message.setText(str(exc));return
        self.outcome={'action':'draft','drafts':{s:{'edits':copy.deepcopy(self.model.edits[s]),'lists':copy.deepcopy(self.model.lists[s]),'day':self.host.job_day,'status':'待核对'} for s in self.codes}}
        self.dirty=False;self.accept()
    def choose_list(self):
        if self.model.invalid:self.message.setText('请先修正当前非法数量');return
        d=StockingDialog(self.host,codes=self.codes)
        if d.exec()==QDialog.DialogCode.Accepted:self.use_list(d.selected)
        d.deleteLater()
    def use_list(self,listing):
        replace=False
        if any(e.get('kind')=='manual' for values in self.model.edits.values() for e in values.values()):
            box=QMessageBox(self);box.setWindowTitle('清单遇到已人工修改商品');box.setText('默认保留已人工修改行（包括人工0）。如选择覆盖，仅覆盖本清单且符合本次库存状态的行。');keep=box.addButton('保留人工修改',QMessageBox.ButtonRole.AcceptRole);over=box.addButton('预览覆盖清单内人工修改',QMessageBox.ButtonRole.ActionRole);cancel=box.addButton('取消',QMessageBox.ButtonRole.RejectRole);box.setDefaultButton(keep);box.exec()
            if box.clickedButton() is cancel:return
            replace=box.clickedButton() is over
        self.model.sequence+=1;self.run(fill_batch,(self.results,copy.deepcopy(self.model.edits),copy.deepcopy(self.model.lists),listing,replace,self.model.sequence),self.filled)
    def filled(self,value):
        if value['pending'] or value['skipped']:
            d=AsyncDialog(self);d.setWindowTitle('清单匹配范围核对');v=QVBoxLayout(d);d.body=QWidget();v.addWidget(d.body);content=QVBoxLayout(d.body);content.addWidget(label(f'待处理{len(value["pending"])}行，按明确条件保留/跳过{len(value["skipped"])}行。缺数据不当作0。'))
            table=QTableWidget(0,3);table.setHorizontalHeaderLabels(['门店','商品编码','未应用原因']);records=value['pending']+value['skipped'];table.setRowCount(len(records));table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            for i,row in enumerate(records):
                for j,k in enumerate(['store','sku','reason']):table.setItem(i,j,QTableWidgetItem(row[k]))
            content.addWidget(table)
            def export_gaps():
                import export_locations,business_excel
                path,_=export_locations.save_file(d,'导出清单未应用明细','铺货清单未应用明细.xlsx','Excel (*.xlsx)')
                if path:d.run(business_excel.write,(path,[('未应用明细',['门店','商品编码','原因'],[[r[k] for k in ('store','sku','reason')] for r in records])]),lambda p:d.message.setText('已导出：'+p))
            content.addWidget(button('导出全部未应用明细',export_gaps));content.addWidget(button('仅填入本次有效匹配范围',d.accept,True));d.setup_async(v);v.addWidget(button('取消整次清单操作',d.reject));d.resize(760,500)
            if d.exec()!=QDialog.DialogCode.Accepted:return
        self.model.replace_edits(value['edits'],value['lists']);self.message.setText('清单已填入当前编辑表；尚未应用到作业。请核对修改与预算。')
    def reject(self):
        if self.task:return super().reject()
        if self.dirty and QMessageBox.question(self,'放弃本次编辑？','已保存的作业和原结果不变。放弃本次未应用修改？',QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)!=QMessageBox.StandardButton.Yes:return
        super().reject()
