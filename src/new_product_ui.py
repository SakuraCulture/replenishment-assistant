"""Virtual first-fill table: filter by source indices; hidden selections survive."""
from PySide6.QtCore import Qt,QAbstractTableModel,QModelIndex,QTimer
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QLineEdit,QComboBox,QTableView,QMessageBox,QStyledItemDelegate
from parameter_ui import NumberInput
from ui_controls import label,button
import new_products as np
import new_product_focus as focus
import result_semantics as sem

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
        self.quantities=[float(selected.get(d['E'],d['BD'] if sem.numeric(d.get('BD')) and d['BD']>0 else 1)) for d in rows]
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
        if col==8:return sem.fmt(self.quantities[i]) if role==Qt.ItemDataRole.DisplayRole else None
        if col==9:return sem.fmt(self.cost(i)) if role==Qt.ItemDataRole.DisplayRole else ''
        return self.static_values(i)[col]
    def cost(self,i):return self.quantities[i]*self.rows[i]['V'] if sem.numeric(self.rows[i].get('V')) else None
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
        if not self.batching:
            if i in self.positions:
                row=self.positions[i];self.dataChanged.emit(self.index(row,col),self.index(row,9 if col==8 else col))
            if self.changed:self.changed()
    def filter(self,indices):
        if indices==self.visible:
            if not self.positions:self.positions={source:row for row,source in enumerate(indices)}
            return
        self.beginResetModel();self.visible=indices;self.positions={source:row for row,source in enumerate(indices)};self.endResetModel()


class QuantityEditor(QStyledItemDelegate):
    def __init__(self,dialog):super().__init__(dialog.table);self.dialog=dialog
    def createEditor(self,parent,option,index):
        editor=NumberInput(parent);editor.setDecimals(8);editor.setRange(0,1e9);editor.setSingleStep(self.dialog.rows[self.dialog.model.visible[index.row()]]['BD']);return editor
    def setEditorData(self,editor,index):editor.setValue(index.data(Qt.ItemDataRole.UserRole))
    def setModelData(self,editor,model,index):model.setData(index,editor.value(),Qt.ItemDataRole.UserRole)


class NewProductDialog(QDialog):
    def __init__(self,parent,base,current,new_focus=None):
        super().__init__(parent);self.setObjectName('newProductDialog');self.setWindowTitle('新品首铺 · '+base['store_name']);self.resize(1180,740)
        self.focused=focus.members(new_focus);self.focus_known=new_focus is not None
        self.rows=[d for d in base['rows'] if np.is_new(d) and sem.work_eligible(d)];self.current=current
        self.cur={d['E']:d for d in current['rows']};self.needles=[(d['E']+' '+str(d.get('D',''))).lower() for d in self.rows]
        selected=current.get('new_products',{}).get('selection',{});v=QVBoxLayout(self)
        v.addWidget(label('月销、可用、在途均为0，缺货天数为0或空白。空白按0识别并保留原值；默认不选，预览应用后生效。禁用/停采商品已筛除。','muted'))
        tools=QHBoxLayout();self.search=QLineEdit();self.search.setPlaceholderText('商品 / 编码');tools.addWidget(self.search,1)
        self.scope=QComboBox();self.scope.addItems(['全部新品','可选','本月新品','往期新品','必卖','已选','未获预算']);self.scope.setCurrentText('可选');tools.addWidget(self.scope)
        self.warehouse=QComboBox();self.warehouse.addItems(['全部仓']+sorted({str(d.get('AU','')) for d in self.rows}));tools.addWidget(self.warehouse)
        self.category=QComboBox();self.category.addItems(['全部分类']+sorted({str(d.get('_category') or '未分类') for d in self.rows}));tools.addWidget(self.category);v.addLayout(tools)
        self.model=NewProductModel(self.rows,selected,self);self.widgets=[(RowSelection(self.model,i),RowQuantity(self.model,i)) for i in range(len(self.rows))]
        self.table=QTableView();self.table.setModel(self.model);self.table.setEditTriggers(QTableView.EditTrigger.DoubleClicked|QTableView.EditTrigger.EditKeyPressed);self.table.setItemDelegateForColumn(8,QuantityEditor(self));v.addWidget(self.table,1)
        for j,width in enumerate([45,160,130,42,65,72,65,66,95,80,260]):self.table.setColumnWidth(j,width)
        self.note=label('','muted');v.addWidget(self.note)
        row=QHBoxLayout();row.addWidget(button('选中当前筛选可选商品',lambda:self.bulk(True)));row.addWidget(button('清空当前筛选选择',lambda:self.bulk(False)));row.addWidget(button('清空全店选择',self.clear_all));row.addStretch();self.preview_btn=button('预览预算变化',self.accept,True);row.addWidget(self.preview_btn);row.addWidget(button('取消',self.reject));v.addLayout(row)
        self.filter_timer=QTimer(self);self.filter_timer.setSingleShot(True);self.filter_timer.setInterval(275);self.filter_timer.timeout.connect(self.refresh)
        self.search.textChanged.connect(lambda _:self.filter_timer.start());self.search.returnPressed.connect(self.refresh)
        for w in [self.scope,self.warehouse,self.category]:w.currentTextChanged.connect(self.refresh)
        self.model.changed=self.refresh;self.refresh()

    def refresh(self,*_):
        self.filter_timer.stop();scope=self.scope.currentText();needle=self.search.text().lower();warehouse=self.warehouse.currentText();category=self.category.currentText()
        indices=[];hidden=0;count=0;total=0
        for i,d in enumerate(self.rows):
            chosen=self.model.selected[i]
            visible=(not needle or needle in self.needles[i]) and warehouse in ('全部仓',str(d.get('AU',''))) and category in ('全部分类',str(d.get('_category') or '未分类'))
            visible=visible and (scope=='全部新品' or scope=='本月新品' and self.focus_known and d['E'] in self.focused or scope=='往期新品' and self.focus_known and d['E'] not in self.focused or scope=='可选' and not self.model.reasons[i] or scope=='必卖' and bool(d.get('_must_sell')) or scope=='已选' and chosen or scope=='未获预算' and bool(self.cur[d['E']].get('_budget_gap',0)))
            if visible:indices.append(i)
            if chosen:count+=1;hidden+=not visible;total+=self.model.cost(i) or 0
        self.model.filter(indices)
        self.note.setText(f'全店已选 {count} 款 · 首铺候选 ¥{total:,.2f} · 筛选外已选 {hidden} 款。新品与动销共同分配总预算；必卖新品优先于普通动销。')

    def bulk(self,on):
        self.refresh();indices=[i for i in self.model.visible if not self.model.reasons[i]]
        money=sum(self.model.cost(i) for i in indices)
        if QMessageBox.question(self,'批量选择',f"对当前筛选 {len(indices)} 款执行{'选中' if on else '清空'}？候选金额 ¥{money:,.2f}",QMessageBox.StandardButton.Ok|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)!=QMessageBox.StandardButton.Ok:return
        self.change_selection(indices,on)

    def change_selection(self,indices,on):
        self.model.batching=True
        try:
            for i in indices:self.widgets[i][0].setChecked(on)
        finally:self.model.batching=False
        self.refresh()
        if self.model.rowCount():self.model.dataChanged.emit(self.model.index(0,0),self.model.index(self.model.rowCount()-1,0))

    def clear_all(self):
        if QMessageBox.question(self,'清空全店','清空全部新品选择，包含筛选外商品？',QMessageBox.StandardButton.Ok|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)!=QMessageBox.StandardButton.Ok:return
        self.change_selection(range(len(self.rows)),False)

    def selections(self):return {d['E']:q.value() for d,(c,q) in zip(self.rows,self.widgets) if c.isChecked()}
