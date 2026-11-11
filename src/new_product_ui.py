"""Virtual first-fill table: filter by source indices; hidden selections survive."""
from PySide6.QtCore import Qt,QAbstractTableModel,QModelIndex,QTimer
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QLineEdit,QComboBox,QTableView,QMessageBox,QStyledItemDelegate
from parameter_ui import NumberInput
from ui_controls import label,button
import new_products as np
import new_product_focus as focus
import result_semantics as sem
import review_filters as filters
import first_fill

HEADERS=['选择','商品','编码','必卖','分类','归属仓','库存单位','最小单位','首铺数量（双击）','估算金额','资格 / 提示']


class RowSelection:
    def __init__(self,model,row):self.model=model;self.row=row
    def isChecked(self):return self.model.selected[self.row]
    def setChecked(self,value):self.model.change(self.row,0,bool(value))
    def isEnabled(self):return not self.model.reasons[self.row]


class RowQuantity:
    def __init__(self,model,row):self.model=model;self.row=row
    def value(self):return self.model.quantities[self.row]
    def setValue(self,value):self.model.change(self.row,8,float(value))


class NewProductModel(QAbstractTableModel):
    def __init__(self,rows,selected,parent):
        super().__init__(parent);self.rows=rows;self.visible=list(range(len(rows)));self.positions={};self.reasons=[np.blocked_reason(d) for d in rows]
        self.selected=[d['E'] in selected for d in rows]
        self.quantities=[];self.quantity_sources=[]
        for d in rows:
            if d['E'] in selected:q,note=float(selected[d['E']]),'沿用本次已维护数量，不自动覆盖'
            elif d['E'] in getattr(parent,'focused',set()):q,note=first_fill.suggestion(d)
            else:q,note=float(d['BD'] if sem.numeric(d.get('BD')) and d['BD']>0 else 1),'往期/未归期新品沿用最小出库单位默认量'
            self.quantities.append(q);self.quantity_sources.append(note)
        self.default_sources=list(self.quantity_sources)
        self.changed=None;self.batching=False;self.cache={}
    def rowCount(self,parent=QModelIndex()):return 0 if parent.isValid() else len(self.visible)
    def columnCount(self,parent=QModelIndex()):return 0 if parent.isValid() else len(HEADERS)
    def headerData(self,section,orientation,role=Qt.ItemDataRole.DisplayRole):
        if role==Qt.ItemDataRole.DisplayRole:return HEADERS[section] if orientation==Qt.Orientation.Horizontal else self.visible[section]+1
    def flags(self,index):
        if not index.isValid():return Qt.ItemFlag.NoItemFlags
        i=self.visible[index.row()];col=index.column();flags=Qt.ItemFlag.ItemIsSelectable
        if col not in (0,8):flags|=Qt.ItemFlag.ItemIsDragEnabled|Qt.ItemFlag.ItemIsDropEnabled|Qt.ItemFlag.ItemIsUserCheckable
        if col not in (0,8) or not self.reasons[i]:flags|=Qt.ItemFlag.ItemIsEnabled
        if col==0:flags|=Qt.ItemFlag.ItemIsUserCheckable
        if col==8 and not self.reasons[i]:flags|=Qt.ItemFlag.ItemIsEditable
        return flags
    def static_values(self,i):
        if i not in self.cache:
            d=self.rows[i];notes='；'.join(d.get('_meta',{}).get('notes',[]))
            reason=self.reasons[i] or ('可选 · 缺箱规按1' if '箱规空白' in notes else '可选 · 按最小单位倍数')
            if d.get('N') in (None,''):reason+='；缺货天数空白按0'
            self.cache[i]={k:sem.fmt(v) for k,v in {1:d.get('D',''),2:d['E'],3:'是' if d.get('_must_sell') else '否',4:d.get('_category'),5:d.get('AU'),6:d.get('G'),7:d.get('BD'),10:reason}.items()}
        return self.cache[i]
    def data(self,index,role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():return None
        i=self.visible[index.row()];col=index.column()
        if col==0:
            if role==Qt.ItemDataRole.CheckStateRole:return Qt.CheckState.Checked if self.selected[i] else Qt.CheckState.Unchecked
            return None
        if col==8 and role==Qt.ItemDataRole.UserRole:return self.quantities[i]
        if role not in (Qt.ItemDataRole.DisplayRole,Qt.ItemDataRole.ToolTipRole):return None
        if col==8:return ('待维护' if self.quantities[i] is None else sem.fmt(self.quantities[i],8)) if role==Qt.ItemDataRole.DisplayRole else self.quantity_sources[i]
        if col==9:return sem.fmt(self.cost(i)) if role==Qt.ItemDataRole.DisplayRole else ''
        if col==10:return self.static_values(i)[col]+'；'+self.quantity_sources[i]+('；'+self.quantity_issue(i) if self.quantity_issue(i) else '')
        return self.static_values(i)[col]
    def quantity_issue(self,i):return first_fill.quantity_issue(self.rows[i],self.quantities[i])
    def cost(self,i):return self.quantities[i]*self.rows[i]['V'] if sem.numeric(self.quantities[i]) and sem.numeric(self.rows[i].get('V')) else None
    def setData(self,index,value,role=Qt.ItemDataRole.EditRole):
        if not index.isValid():return False
        i=self.visible[index.row()]
        if self.reasons[i]:return False
        if index.column()==0 and role==Qt.ItemDataRole.CheckStateRole:self.change(i,0,value==Qt.CheckState.Checked or value==Qt.CheckState.Checked.value);return True
        if index.column()==8 and role==Qt.ItemDataRole.UserRole:self.change(i,8,float(value));return True
        return False
    def change(self,i,col,value):
        target=self.selected if col==0 else self.quantities
        if target[i]==value:return
        target[i]=value
        if col==8:self.quantity_sources[i]='本次数量已人工维护；原默认依据：'+self.default_sources[i]
        if not self.batching:
            if i in self.positions:
                row=self.positions[i];self.dataChanged.emit(self.index(row,col),self.index(row,10 if col==8 else col))
            if self.changed:self.changed()
    def filter(self,indices):
        if indices==self.visible:
            if not self.positions:self.positions={source:row for row,source in enumerate(indices)}
            return
        self.beginResetModel();self.visible=indices;self.positions={source:row for row,source in enumerate(indices)};self.endResetModel()


class QuantityEditor(QStyledItemDelegate):
    def __init__(self,dialog):super().__init__(dialog.table);self.dialog=dialog
    def createEditor(self,parent,option,index):
        editor=NumberInput(parent);editor.source_row=self.dialog.model.visible[index.row()];editor.setDecimals(8);editor.setRange(0,1e9);editor.setSingleStep(self.dialog.rows[editor.source_row]['BD']);return editor
    def setEditorData(self,editor,index):editor.setValue(index.data(Qt.ItemDataRole.UserRole) or 0)
    def setModelData(self,editor,model,index):model.change(editor.source_row,8,editor.value())


class NewProductDialog(QDialog):
    def __init__(self,parent,base,current,new_focus=None):
        super().__init__(parent);self.setObjectName('newProductDialog');self.setWindowTitle('新品首铺 · '+base['store_name']);self.resize(1180,740)
        self.focused=focus.members(new_focus);self.focus_known=new_focus is not None
        self.rows=[d for d in base['rows'] if np.is_new(d) and sem.work_eligible(d)];self.current=current
        self.cur={d['E']:d for d in current['rows']};self.needles=[filters.search_text(d) for d in self.rows]
        selected=current.get('new_products',{}).get('selection',{});v=QVBoxLayout(self)
        v.addWidget(label('月销、可用、在途均为0，缺货天数为0或空白。空白按0识别并保留原值；默认不选，预览应用后生效。禁用/停采商品已筛除。','muted'))
        hint=label('本月新品的首铺数量默认使用归属仓商品资料“新店新品首铺数”（库存单位）；已维护数量保留。来源缺失或箱规不符请在“资料待核”中维护，不自动补成1箱。','muted');hint.setWordWrap(True);v.addWidget(hint)
        tools=QHBoxLayout();self.search=QLineEdit();self.search.setPlaceholderText('商品名称 / 编码 / 条码；空格分词');self.search.setClearButtonEnabled(True);tools.addWidget(self.search,1)
        self.scope=QComboBox();self.scope.addItems(['全部新品','可选','资料待核']);self.scope.setCurrentText('可选');tools.addWidget(self.scope)
        self.warehouse=QComboBox();self.warehouse.addItems(['全部仓']+sorted({str(d.get('AU','')) for d in self.rows}));tools.addWidget(self.warehouse)
        self.category=QComboBox();self.category.addItems(['全部分类']+sorted({str(d.get('_category') or '未分类') for d in self.rows}));tools.addWidget(self.category);v.addLayout(tools)
        selectors=QHBoxLayout();self.group=QComboBox();self.group.addItems(['全部新品','本月新品','往期新品','新品归期待核']);selectors.addWidget(label('分组'));selectors.addWidget(self.group)
        self.selection_filter=QComboBox();self.selection_filter.addItems(['全部选择状态','已选','未选']);selectors.addWidget(self.selection_filter)
        self.priority_filter=QComboBox();self.priority_filter.addItems(['全部优先级','仅必卖']);selectors.addWidget(self.priority_filter)
        self.budget_filter=QComboBox();self.budget_filter.addItems(['全部预算状态','无预算缺口','部分安排','预算暂缓','未启用预算']);selectors.addWidget(self.budget_filter);selectors.addWidget(button('重置筛选',self.reset_filters));selectors.addStretch();v.addLayout(selectors)
        self.model=NewProductModel(self.rows,selected,self);self.widgets=[(RowSelection(self.model,i),RowQuantity(self.model,i)) for i in range(len(self.rows))]
        self.table=QTableView();self.table.setModel(self.model);self.table.setEditTriggers(QTableView.EditTrigger.DoubleClicked|QTableView.EditTrigger.EditKeyPressed);self.table.setItemDelegateForColumn(8,QuantityEditor(self));v.addWidget(self.table,1)
        for j,width in enumerate([45,160,130,42,65,72,65,66,95,80,260]):self.table.setColumnWidth(j,width)
        self.note=label('','muted');v.addWidget(self.note)
        row=QHBoxLayout();row.addWidget(button('选中当前筛选可选商品',lambda:self.bulk(True)));row.addWidget(button('清空当前筛选选择',lambda:self.bulk(False)));row.addStretch();self.clear_btn=button('清空全店选择',self.clear_all);self.clear_btn.setStyleSheet('color:#A52A2A;');row.addWidget(self.clear_btn);self.undo_selection=None;self.undo_btn=button('撤销上次批量选择',self.undo_bulk);self.undo_btn.setEnabled(False);row.addWidget(self.undo_btn);self.preview_btn=button('预览预算变化',self.review,True);row.addWidget(self.preview_btn);row.addWidget(button('取消',self.reject));v.addLayout(row)
        self.filter_timer=QTimer(self);self.filter_timer.setSingleShot(True);self.filter_timer.setInterval(275);self.filter_timer.timeout.connect(self.refresh)
        self.search.textChanged.connect(lambda _:self.filter_timer.start());self.search.returnPressed.connect(self.refresh)
        for w in [self.scope,self.warehouse,self.category,self.group,self.selection_filter,self.priority_filter,self.budget_filter]:w.currentTextChanged.connect(self.refresh)
        self.model.changed=lambda:self.filter_timer.start(0);self.refresh()

    def reset_filters(self):
        from PySide6.QtCore import QSignalBlocker
        controls=[self.scope,self.warehouse,self.category,self.group,self.selection_filter,self.priority_filter,self.budget_filter,self.search];locks=[QSignalBlocker(c) for c in controls]
        for c in controls[:-1]:c.setCurrentIndex(0)
        self.scope.setCurrentText('可选');self.search.clear();del locks;self.refresh()

    def refresh(self,*_):
        if hasattr(self,'table') and self.table.state()==QTableView.State.EditingState:self.filter_timer.start(50);return
        self.filter_timer.stop();scope=self.scope.currentText();needle=self.search.text().lower();warehouse=self.warehouse.currentText();category=self.category.currentText()
        indices=[];hidden=0;count=0;total=0
        for i,d in enumerate(self.rows):
            chosen=self.model.selected[i]
            visible=filters.matches(self.needles[i],needle) and warehouse in ('全部仓',str(d.get('AU',''))) and category in ('全部分类',str(d.get('_category') or '未分类'))
            issue=self.model.reasons[i] or self.model.quantity_issue(i)
            visible=visible and (scope=='全部新品' or scope=='可选' and not issue or scope=='资料待核' and bool(issue))
            visible=visible and (self.group.currentIndex()==0 or filters.group(self.cur[d['E']],self.focused,self.focus_known)==self.group.currentText())
            visible=visible and (self.selection_filter.currentIndex()==0 or chosen==(self.selection_filter.currentText()=='已选'))
            visible=visible and filters.accepts(self.cur[d['E']],{'budget':self.budget_filter.currentText(),'must':self.priority_filter.currentText()})
            if visible:indices.append(i)
            if chosen:count+=1;hidden+=not visible;total+=self.model.cost(i) or 0
        self.model.filter(indices)
        self.clear_btn.setText(f'清空全店选择（{count}款）')
        pending=sum(bool(self.model.quantity_issue(i)) and not self.model.reasons[i] for i in range(len(self.rows)))
        self.note.setText(f'当前筛选 {len(indices)} 款 · 全店已选 {count} 款 · 首铺候选 ¥{total:,.2f}（预算前，待维护款不计） · 数量待维护 {pending} 款 · 筛选外已选 {hidden} 款。预算状态为当前已应用结果；本次选择须预览后生效。')

    def bulk(self,on):
        self.refresh();indices=[i for i in self.model.visible if not self.model.reasons[i] and (not on or not self.model.quantity_issue(i))]
        money=sum(self.model.cost(i) or 0 for i in indices)
        if QMessageBox.question(self,'批量选择',f"对当前筛选 {len(indices)} 款执行{'选中' if on else '清空'}？候选金额 ¥{money:,.2f}",QMessageBox.StandardButton.Ok|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)!=QMessageBox.StandardButton.Ok:return
        self.change_selection(indices,on)

    def change_selection(self,indices,on):
        self.undo_selection=list(self.model.selected);self.undo_btn.setEnabled(True)
        self.model.batching=True
        try:
            for i in indices:self.widgets[i][0].setChecked(on)
        finally:self.model.batching=False
        self.refresh()
        if self.model.rowCount():self.model.dataChanged.emit(self.model.index(0,0),self.model.index(self.model.rowCount()-1,0))

    def undo_bulk(self):
        if self.undo_selection is None:return
        self.model.selected[:]=self.undo_selection;self.undo_selection=None;self.undo_btn.setEnabled(False);self.refresh()
        if self.model.rowCount():self.model.dataChanged.emit(self.model.index(0,0),self.model.index(self.model.rowCount()-1,0))

    def clear_all(self):
        if QMessageBox.question(self,'清空全店','清空全部新品选择，包含筛选外商品？',QMessageBox.StandardButton.Ok|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)!=QMessageBox.StandardButton.Ok:return
        self.change_selection(range(len(self.rows)),False)

    def review(self):
        issues=[d['E']+'：'+(self.model.reasons[i] or self.model.quantity_issue(i)) for i,d in enumerate(self.rows) if self.model.selected[i] and (self.model.reasons[i] or self.model.quantity_issue(i))]
        if issues:
            box=QMessageBox(self);box.setIcon(QMessageBox.Icon.Warning);box.setWindowTitle('首铺数量需要维护');box.setText(f'所选商品中有{len(issues)}款需要核对，尚未应用。');box.setInformativeText('\n'.join(issues[:6]));box.setDetailedText('\n'.join(issues));box.exec();return
        self.accept()

    def selections(self):return {d['E']:q.value() for d,(c,q) in zip(self.rows,self.widgets) if c.isChecked()}
