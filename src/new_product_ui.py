"""Store-local selection dialog: filters never discard hidden selections."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QLineEdit,QComboBox,QTableWidget,QTableWidgetItem,QMessageBox,QStyledItemDelegate
from parameter_ui import NumberInput
from ui_controls import label,button
import new_products as np
import new_product_focus as focus
import result_semantics as sem


class RowSelection:
    """Item state; no permanent QWidget for each of thousands of rows."""
    def __init__(self,item):self.item=item
    def isChecked(self):return self.item.checkState()==Qt.CheckState.Checked
    def setChecked(self,value):self.item.setCheckState(Qt.CheckState.Checked if value else Qt.CheckState.Unchecked)
    def isEnabled(self):return bool(self.item.flags() & Qt.ItemFlag.ItemIsEnabled)


class RowQuantity:
    def __init__(self,item):self.item=item
    def value(self):return self.item.data(Qt.ItemDataRole.UserRole)
    def setValue(self,value):self.item.setData(Qt.ItemDataRole.UserRole,float(value))


class QuantityEditor(QStyledItemDelegate):
    def __init__(self,dialog):super().__init__(dialog.table);self.dialog=dialog
    def createEditor(self,parent,option,index):
        editor=NumberInput(parent);editor.setDecimals(8);editor.setRange(0,1e9);editor.setSingleStep(self.dialog.rows[index.row()]['BD']);return editor
    def setEditorData(self,editor,index):editor.setValue(index.data(Qt.ItemDataRole.UserRole))
    def setModelData(self,editor,model,index):model.setData(index,editor.value(),Qt.ItemDataRole.UserRole)


class NewProductDialog(QDialog):
    def __init__(self,parent,base,current,new_focus=None):
        super().__init__(parent);self.setObjectName('newProductDialog');self.setWindowTitle('新品首铺 · '+base['store_name']);self.resize(1180,740)
        self.focused=focus.members(new_focus);self.focus_known=new_focus is not None;self.rows=[d for d in base['rows'] if np.is_new(d)];self.widgets=[];self.current=current
        selected=current.get('new_products',{}).get('selection',{});v=QVBoxLayout(self)
        v.addWidget(label('月销、可用、在途均为0，缺货天数为0或空白。空白按0识别并保留原值；默认不选，预览应用后生效。','muted'))
        tools=QHBoxLayout();self.search=QLineEdit();self.search.setPlaceholderText('商品 / 编码');tools.addWidget(self.search,1)
        self.scope=QComboBox();self.scope.addItems(['全部新品','可选','本月新品','往期新品','必卖','已选','未获预算']);self.scope.setCurrentText('可选');tools.addWidget(self.scope)
        self.warehouse=QComboBox();self.warehouse.addItems(['全部仓']+sorted({str(d.get('AU','')) for d in self.rows}));tools.addWidget(self.warehouse)
        self.category=QComboBox();self.category.addItems(['全部分类']+sorted({str(d.get('_category') or '未分类') for d in self.rows}));tools.addWidget(self.category);v.addLayout(tools)
        self.table=QTableWidget(len(self.rows),11);self.table.setHorizontalHeaderLabels(['选择','商品','编码','必卖','分类','归属仓','库存单位','最小单位','首铺数量（双击）','估算金额','资格 / 提示']);self.table.setEditTriggers(QTableWidget.EditTrigger.DoubleClicked|QTableWidget.EditTrigger.EditKeyPressed);self.table.setItemDelegateForColumn(8,QuantityEditor(self));v.addWidget(self.table,1)
        for j,width in enumerate([45,160,130,42,65,72,65,66,95,80,260]):self.table.setColumnWidth(j,width)
        for i,d in enumerate(self.rows):
            reason=np.blocked_reason(d);choice=QTableWidgetItem();choice.setFlags(Qt.ItemFlag.ItemIsUserCheckable|Qt.ItemFlag.ItemIsSelectable|(Qt.ItemFlag.ItemIsEnabled if not reason else Qt.ItemFlag.NoItemFlags));check=RowSelection(choice);check.setChecked(d['E'] in selected)
            quantity=QTableWidgetItem();quantity.setFlags(Qt.ItemFlag.ItemIsSelectable|(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsEditable if not reason else Qt.ItemFlag.NoItemFlags));qty=RowQuantity(quantity);step=d.get('BD');step=step if sem.numeric(step) and step>0 else 1;qty.setValue(selected.get(d['E'],step))
            self.widgets.append((check,qty));self.table.setItem(i,0,choice);self.table.setItem(i,8,quantity)
            notes='；'.join(d.get('_meta',{}).get('notes',[]))
            vals={1:d.get('D',''),2:d['E'],3:'是' if d.get('_must_sell') else '否',4:d.get('_category'),5:d.get('AU'),6:d.get('G'),7:d.get('BD'),9:'',10:reason or ('可选 · 缺箱规按1' if '箱规空白' in notes else '可选 · 按最小单位倍数')}
            if d.get('N') in (None,''):vals[10]+='；缺货天数空白按0'
            for j,val in vals.items():
                item=QTableWidgetItem(sem.fmt(val));item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable);item.setToolTip(sem.fmt(val));self.table.setItem(i,j,item)
        self.note=label('','muted');v.addWidget(self.note)
        row=QHBoxLayout();row.addWidget(button('选中当前筛选可选商品',lambda:self.bulk(True)));row.addWidget(button('清空当前筛选选择',lambda:self.bulk(False)));row.addWidget(button('清空全店选择',self.clear_all));row.addStretch();self.preview_btn=button('预览预算变化',self.accept,True);row.addWidget(self.preview_btn);row.addWidget(button('取消',self.reject));v.addLayout(row)
        self.search.textChanged.connect(self.refresh)
        self.table.itemChanged.connect(self.refresh)
        for w in [self.scope,self.warehouse,self.category]:w.currentTextChanged.connect(self.refresh)
        self.refresh()

    def refresh(self,*_):
        previous=self.table.blockSignals(True);self.table.setUpdatesEnabled(False)
        hidden=0;count=0;total=0;cur={d['E']:d for d in self.current['rows']}
        for i,(d,(check,qty)) in enumerate(zip(self.rows,self.widgets)):
            scope=self.scope.currentText();visible=(not self.search.text() or self.search.text().lower() in (d['E']+' '+str(d.get('D',''))).lower())
            visible &= self.warehouse.currentText() in ('全部仓',str(d.get('AU','')))
            visible &= self.category.currentText() in ('全部分类',str(d.get('_category') or '未分类'))
            visible &= scope=='全部新品' or scope=='本月新品' and self.focus_known and d['E'] in self.focused or scope=='往期新品' and self.focus_known and d['E'] not in self.focused or scope=='可选' and check.isEnabled() or scope=='必卖' and bool(d.get('_must_sell')) or scope=='已选' and check.isChecked() or scope=='未获预算' and bool(cur[d['E']].get('_budget_gap',0))
            self.table.setRowHidden(i,not visible)
            self.table.item(i,8).setText(sem.fmt(qty.value()))
            cost=qty.value()*d['V'] if sem.numeric(d.get('V')) else None;self.table.item(i,9).setText(sem.fmt(cost))
            if check.isChecked():count+=1;hidden+=not visible;total+=cost or 0
        self.note.setText(f'全店已选 {count} 款 · 首铺候选 ¥{total:,.2f} · 筛选外已选 {hidden} 款。新品与动销共同分配总预算；必卖新品优先于普通动销。')
        self.table.blockSignals(previous);self.table.setUpdatesEnabled(True)

    def bulk(self,on):
        indices=[i for i,(c,q) in enumerate(self.widgets) if c.isEnabled() and not self.table.isRowHidden(i)]
        money=sum(self.widgets[i][1].value()*self.rows[i]['V'] for i in indices)
        if QMessageBox.question(self,'批量选择',f"对当前筛选 {len(indices)} 款执行{'选中' if on else '清空'}？候选金额 ¥{money:,.2f}",QMessageBox.StandardButton.Ok|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)!=QMessageBox.StandardButton.Ok:return
        # Freeze indices: checking a row can itself change the visible filter.
        previous=self.table.blockSignals(True)
        try:
            for i in indices:self.widgets[i][0].setChecked(on)
        finally:self.table.blockSignals(previous)
        self.refresh()

    def clear_all(self):
        if QMessageBox.question(self,'清空全店', '清空全部新品选择，包含筛选外商品？',QMessageBox.StandardButton.Ok|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)!=QMessageBox.StandardButton.Ok:return
        previous=self.table.blockSignals(True)
        try:
            for c,q in self.widgets:c.setChecked(False)
        finally:self.table.blockSignals(previous)
        self.refresh()

    def selections(self):return {d['E']:q.value() for d,(c,q) in zip(self.rows,self.widgets) if c.isChecked()}
