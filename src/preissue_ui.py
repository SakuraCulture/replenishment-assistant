"""One final list: exact pack editor, list fill, measured preview, scoped approval."""
import copy
from decimal import Decimal
from PySide6.QtCore import Qt,QAbstractTableModel,QModelIndex,QPersistentModelIndex,QSortFilterProxyModel,QTimer
from PySide6.QtGui import QValidator,QColor
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QTableView,QLineEdit,QComboBox,QCheckBox,
    QDialog,QMessageBox,QAbstractSpinBox,QStyledItemDelegate,QTableWidget,QTableWidgetItem,QSplitter,QLabel)
from ui_controls import button,label
from stocking_ui import AsyncDialog,StockingDialog
from runtime_support import checkpoint
from export_progress import RowProgress,prefixed
import preissue as rules
import result_semantics as sem
import work_repository as wr

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
        self.searches=[(' '.join([s,results[s]['store_name'],str(d.get('D','')),d['E']])).lower() for s,d in self.entries]
        self.blocks=[rules.blocked(d) for s,d in self.entries]
        self.positions={self.key(i):i for i in range(len(self.entries))}
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
        note=self.invalid.get(key) or self.blocks[i] or self.risks.get(key) or ('请填写调整原因' if e and not e.get('reason') else '')
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
            self.dataChanged.emit(index,index);self.changed(False);return True
        if self.blocks[i] or index.column() not in (9,13):return False
        try:q=rules.quantity(value,d['BD']) if index.column()==9 else self.qty(i)
        except ValueError as exc:
            self.invalid[key]=str(exc);self.dataChanged.emit(self.index(i,9),self.index(i,14));self.changed(True);return False
        prev=self.edits[s].get(d['E'],{});reason=str(value).strip() if index.column()==13 else prev.get('reason') or self.reason
        self.edits[s][d['E']]={**prev,'quantity':q,'kind':'manual','reason':reason,'warehouse':d['AU'],'sequence':self.sequence,'edited_at':wr.now()}
        self.sequence+=1;self.invalid.pop(key,None);self.dataChanged.emit(self.index(i,9),self.index(i,14));self.changed(True);return True
    def replace_edits(self,updates,lists=None,cleared=()):
        self.beginResetModel()
        for s,e in updates.items():self.edits[s]=copy.deepcopy(e)
        if lists:
            for s,l in lists.items():self.lists[s]=copy.deepcopy(l)
        for key in cleared:self.invalid.pop(key,None)
        self.risks.clear();self.endResetModel();self.changed(True)
    def notify(self,first=0,last=None):
        if self.entries:self.dataChanged.emit(self.index(0,first),self.index(len(self.entries)-1,self.columnCount()-1 if last is None else last))


class FinalFilter(QSortFilterProxyModel):
    def __init__(self,parent):
        super().__init__(parent);self.needle='';self.store='';self.warehouse='';self.mode='全部'
        # Source data may change inside delegate commit. Rebuild only after the
        # editor has closed, using Qt's full invalidation protocol.
        self.setDynamicSortFilter(False)
    def filterAcceptsRow(self,i,parent):
        m=self.sourceModel();s,d=m.entries[i]
        if any(word not in m.searches[i] for word in self.needle.split()) or self.store and s!=self.store or self.warehouse and d.get('AU')!=self.warehouse:return False
        q=m.qty(i);key=(s,d['E'])
        return (self.mode=='全部' or self.mode=='可调整' and not m.blocks[i] or self.mode=='已修改' and d['E'] in m.edits[s] or self.mode=='拟签发大于0' and sem.numeric(q) and q>0 or
                self.mode=='拟签发为0' and q==0 or self.mode=='资料待核' and bool(m.blocks[i]) or self.mode=='风险待确认' and key in m.risks and key not in m.confirmed or self.mode=='已勾选' and key in m.checked)


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
        top=QHBoxLayout();self.search=QLineEdit();self.search.setPlaceholderText('商品编码 / 名称 / 门店');top.addWidget(self.search,1)
        self.stores=QComboBox();self.stores.addItem('全部所选门店','')
        for s in codes:self.stores.addItem(self.results[s]['store_name'],s)
        top.addWidget(self.stores);self.warehouses=QComboBox();self.warehouses.addItem('全部仓','')
        for wh in dict.fromkeys(d.get('AU','') for r in self.results.values() for d in r['rows']):self.warehouses.addItem(wh,wh)
        top.addWidget(self.warehouses);self.scope=QComboBox();self.scope.addItems(['可调整','全部','已修改','拟签发大于0','拟签发为0','资料待核','风险待确认','已勾选']);top.addWidget(self.scope);b.addLayout(top)
        actions=QHBoxLayout();actions.addWidget(button('按清单补足',self.choose_list));actions.addWidget(button('勾选当前筛选',lambda:self.check_visible(True)));actions.addWidget(button('清空全部勾选',self.clear_checks));actions.addWidget(button('批量设数量',self.bulk));actions.addWidget(button('恢复所选自动数量',self.restore));actions.addWidget(button('放弃所选门店全部调整',self.restore_all));b.addLayout(actions)
        reasons=QHBoxLayout();reasons.addWidget(label('修改原因'));self.reason=QComboBox();self.reason.addItems(['请选择原因','活动备货','陈列需要','经营判断','暂不补货','其他']);reasons.addWidget(self.reason);self.reason_text=QLineEdit();self.reason_text.setPlaceholderText('其他原因必填；也可逐行编辑原因');reasons.addWidget(self.reason_text,1);reasons.addWidget(button('填入所选已修改行',self.assign_reason));b.addLayout(reasons)
        self.model=FinalModel(self.results,getattr(host,'preissue_drafts',{}),self.changed,self);self.proxy=FinalFilter(self);self.proxy.setSourceModel(self.model);self.proxy.setSortRole(Qt.ItemDataRole.UserRole)
        self.refresh_timer=QTimer(self);self.refresh_timer.setSingleShot(True);self.refresh_timer.timeout.connect(self.refresh_after_edit)
        self.table=QTableView();self.table.setModel(self.proxy);self.table.setSortingEnabled(True);self.table.sortByColumn(-1,Qt.SortOrder.AscendingOrder);self.table.setItemDelegateForColumn(9,PackDelegate(self.table));self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableView.EditTrigger.DoubleClicked|QTableView.EditTrigger.EditKeyPressed)
        self.splitter=QSplitter(Qt.Orientation.Vertical);self.splitter.addWidget(self.table);self.review=QTableWidget(0,4);self.review.setHorizontalHeaderLabels(['确认','门店 / 商品','约束类型','内容']);self.review.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);self.splitter.addWidget(self.review);self.splitter.setSizes([430,130]);b.addWidget(self.splitter,1)
        for i,width in enumerate([45,140,80,145,180,60,60,85,75,95,60,95,90,125,220]):self.table.setColumnWidth(i,width)
        self.review.setColumnWidth(0,48);self.review.setColumnWidth(1,220);self.review.setColumnWidth(2,100);self.review.setColumnWidth(3,850)
        self.metrics=label('','muted');b.addWidget(self.metrics)
        self.budget_note=label('','muted');b.addWidget(self.budget_note)
        bottom=QHBoxLayout();self.confirm_all=QCheckBox('已核对并确认本次列出的所有例外');bottom.addWidget(self.confirm_all);bottom.addStretch();self.preview_btn=button('核对修改与预算',self.preview,True);bottom.addWidget(self.preview_btn);self.apply_btn=button('应用最终数量',self.apply,True);self.apply_btn.setEnabled(False);bottom.addWidget(self.apply_btn);bottom.addWidget(button('保留为待核对草稿',self.keep_draft));b.addLayout(bottom)
        self.setup_async(v);v.addWidget(button('取消 / 放弃本次编辑',self.reject))
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.setInterval(250);self.timer.timeout.connect(self.filter);self.search.textChanged.connect(lambda:self.timer.start())
        for w in (self.stores,self.warehouses,self.scope):w.currentIndexChanged.connect(self.filter)
        self.reason.currentIndexChanged.connect(self.reason_changed);self.reason_text.textChanged.connect(self.reason_changed);self.confirm_all.toggled.connect(self.check_risks);self.review.itemChanged.connect(self.risk_checked)
        self.filter()
        if listing:QTimer.singleShot(0,lambda:self.use_list(listing))
    def reason_changed(self,*_):self.model.reason=self.reason_text.text().strip() if self.reason.currentText()=='其他' else self.reason.currentText() if self.reason.currentIndex()>0 else ''
    def filter(self,*_):
        self.timer.stop();self.proxy.needle=self.search.text().lower();self.proxy.store=self.stores.currentData();self.proxy.warehouse=self.warehouses.currentData();self.proxy.mode=self.scope.currentText();self.proxy.invalidateFilter();self.changed(False)
    def changed(self,edited=True):
        if edited:
            self.dirty=True;self.prepared=None;self.apply_btn.setEnabled(False);self.review.setRowCount(0);self.confirm_all.setChecked(False)
            self.model.risks.clear();self.model.confirmed.clear();self.model.reviewed=False;self.model.notify(14,14)
            self.message.setText('有修改，待重新核对；当前金额为未应用预览。')
        if hasattr(self,'refresh_timer'):self.refresh_timer.start(0)
        self.update_metrics()
    def refresh_after_edit(self):
        if self.table.state()==QTableView.State.EditingState:
            self.refresh_timer.start(50);return
        self.proxy.invalidate();self.update_metrics()
    def update_metrics(self):
        shown={self.model.key(self.proxy.mapToSource(self.proxy.index(i,0)).row()) for i in range(self.proxy.rowCount())}
        count=sum(len(v) for v in self.model.edits.values())
        self.metrics.setText(f'所选{len(self.codes)}店 · 显示{self.proxy.rowCount()}行 · 已勾选{len(self.model.checked)}行（筛选外{len(self.model.checked-shown)}行） · 调整记录{count}条 · 非法输入{len(self.model.invalid)}行。箱规24只能填0/24/48…')
        summaries=[]
        for s,r in self.results.items():
            old=r.get('preissue',{}).get('original_summary',r['summary'])['amount'];amount=rules.decimal(old)
            for sku,e in self.model.edits[s].items():
                i=self.model.positions.get((s,sku))
                if i is not None and not self.model.blocks[i]:amount+=(rules.decimal(e['quantity'])-rules.decimal(self.model.automatic(i)))*rules.decimal(self.model.entries[i][1]['V'])
            limit=r['budget'].get('limit') if r['budget'].get('enabled') else None
            detail=f'目标{sem.fmt(limit)}元；剩余{sem.fmt(float(rules.decimal(limit)-amount))}元' if limit is not None else '不限预算'
            summaries.append(f'{r["store_name"]}：自动{old:,.2f} → 拟签发{amount:,.2f}元（{detail}）')
        self.budget_note.setText('；'.join(summaries)+'。金额预览供核对，签发前仍须通过整店预算校验。')
    def check_visible(self,on):
        for i in range(self.proxy.rowCount()):self.model.checked.add(self.model.key(self.proxy.mapToSource(self.proxy.index(i,0)).row()))
        self.model.notify(0,0);self.changed(False)
    def clear_checks(self):self.model.checked.clear();self.model.notify(0,0);self.changed(False)
    def chosen(self):return [i for i in range(len(self.model.entries)) if self.model.key(i) in self.model.checked]
    def assign_reason(self):
        if not self.model.reason:self.message.setText('请先选择或填写原因');return
        for i in self.chosen():
            s,d=self.model.entries[i]
            if d['E'] in self.model.edits[s]:self.model.edits[s][d['E']].update(reason=self.model.reason,edited_at=wr.now())
        self.model.notify(13,14);self.changed(True)
    def bulk(self):
        indices=self.chosen()
        if not indices:self.message.setText('请先勾选需要修改的商品');return
        d=QDialog(self);d.setWindowTitle('批量设数量 · 仅所选商品');v=QVBoxLayout(d);mode=QComboBox();mode.addItems(['每款设N个最小出库单位','设置相同库存单位数量']);v.addWidget(mode);qty=PackInput(1);v.addWidget(qty);v.addWidget(label(f'将修改{len(indices)}行；每行按本仓箱规校验，任意一行非法则整次不应用。'))
        v.addWidget(button('预览并填入',d.accept,True));v.addWidget(button('取消',d.reject))
        if d.exec()!=QDialog.DialogCode.Accepted:return
        try:
            value=qty.value();updates=copy.deepcopy(self.model.edits)
            for i in indices:
                s,row=self.model.entries[i]
                if self.model.blocks[i]:raise ValueError(row['E']+'：'+self.model.blocks[i])
                q=rules.quantity(rules.decimal(value)*rules.decimal(row['BD']) if mode.currentIndex()==0 else value,row['BD'])
                updates[s][row['E']]={**updates[s].get(row['E'],{}),'quantity':q,'kind':'manual','reason':self.model.reason,'warehouse':row['AU'],'sequence':self.model.sequence,'edited_at':wr.now()};self.model.sequence+=1
            text='\n'.join(f'{self.model.entries[i][0]} / {self.model.entries[i][1]["E"]}：{sem.fmt(self.model.qty(i))} → {updates[self.model.entries[i][0]][self.model.entries[i][1]["E"]]["quantity"]}' for i in indices[:20])
            if QMessageBox.question(self,'确认批量填数',text+(f'\n共{len(indices)}行，仅展示前20行；填入后可按已修改筛选逐行核对。' if len(indices)>20 else ''),QMessageBox.StandardButton.Ok|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)!=QMessageBox.StandardButton.Ok:return
            self.model.replace_edits(updates,cleared=[self.model.key(i) for i in indices])
        except Exception as exc:self.message.setText('未应用：'+str(exc))
    def restore(self):
        indices=self.chosen()
        if not indices:self.message.setText('请勾选要恢复的商品');return
        if QMessageBox.question(self,'恢复所选自动数量',f'恢复{len(indices)}行原自动数量并清除这些行的清单/人工覆盖？其余行保留。',QMessageBox.StandardButton.Ok|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)!=QMessageBox.StandardButton.Ok:return
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
        self.review.blockSignals(False);self.confirm_all.setChecked(False);self.model.notify(14,14);self.message.setText('；'.join(summary));self.risk_checked()
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
