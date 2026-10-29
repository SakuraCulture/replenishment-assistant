"""Store-local selection dialog: filters never discard hidden selections."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QLineEdit,QComboBox,QTableWidget,QTableWidgetItem,QCheckBox,QHeaderView,QMessageBox
from parameter_ui import NumberInput
from ui_controls import label,button
import new_products as np
import result_semantics as sem


class NewProductDialog(QDialog):
    def __init__(self,parent,base,current):
        super().__init__(parent);self.setObjectName('newProductDialog');self.setWindowTitle('新品首铺 · '+base['store_name']);self.resize(1180,740)
        self.rows=[d for d in base['rows'] if np.is_new(d)];self.widgets=[];self.current=current
        selected=current.get('new_products',{}).get('selection',{});v=QVBoxLayout(self)
        v.addWidget(label('四项为0：月销、可用、在途、原缺货天数。默认不选；选择只在预览后点击“应用”才生效。','muted'))
        tools=QHBoxLayout();self.search=QLineEdit();self.search.setPlaceholderText('商品 / 编码');tools.addWidget(self.search,1)
        self.scope=QComboBox();self.scope.addItems(['全部新品','可选','必卖','已选','未获预算']);tools.addWidget(self.scope)
        self.warehouse=QComboBox();self.warehouse.addItems(['全部仓']+sorted({str(d.get('AU','')) for d in self.rows}));tools.addWidget(self.warehouse)
        self.category=QComboBox();self.category.addItems(['全部分类']+sorted({str(d.get('_category') or '未分类') for d in self.rows}));tools.addWidget(self.category);v.addLayout(tools)
        self.table=QTableWidget(len(self.rows),11);self.table.setHorizontalHeaderLabels(['选择','商品','编码','必卖','分类','归属仓','库存单位','最小单位','首铺数量','估算金额','资格 / 提示']);self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);self.table.setColumnWidth(1,190);self.table.setColumnWidth(10,280);v.addWidget(self.table,1)
        for j,width in enumerate([45,160,130,42,65,72,65,66,95,80,260]):self.table.setColumnWidth(j,width)
        for i,d in enumerate(self.rows):
            reason=np.blocked_reason(d);check=QCheckBox();check.setChecked(d['E'] in selected);check.setEnabled(not reason)
            qty=NumberInput();qty.setDecimals(8);qty.setRange(0,1e9);step=d.get('BD');step=step if sem.numeric(step) and step>0 else 1;qty.setSingleStep(step);qty.setValue(selected.get(d['E'],step));qty.setEnabled(not reason)
            self.widgets.append((check,qty));self.table.setCellWidget(i,0,check);self.table.setCellWidget(i,8,qty)
            notes='；'.join(d.get('_meta',{}).get('notes',[]))
            vals={1:d.get('D',''),2:d['E'],3:'是' if d.get('_must_sell') else '否',4:d.get('_category'),5:d.get('AU'),6:d.get('G'),7:d.get('BD'),9:'',10:reason or ('可选 · 缺箱规按1' if '箱规空白' in notes else '可选 · 按最小单位倍数')}
            for j,val in vals.items():
                item=QTableWidgetItem(sem.fmt(val));item.setToolTip(sem.fmt(val));self.table.setItem(i,j,item)
            check.toggled.connect(self.refresh);qty.valueChanged.connect(self.refresh)
        self.note=label('','muted');v.addWidget(self.note)
        row=QHBoxLayout();row.addWidget(button('选中当前筛选可选商品',lambda:self.bulk(True)));row.addWidget(button('清空当前筛选选择',lambda:self.bulk(False)));row.addWidget(button('清空全店选择',self.clear_all));row.addStretch();self.preview_btn=button('预览预算变化',self.accept,True);row.addWidget(self.preview_btn);row.addWidget(button('取消',self.reject));v.addLayout(row)
        self.search.textChanged.connect(self.refresh)
        for w in [self.scope,self.warehouse,self.category]:w.currentTextChanged.connect(self.refresh)
        self.refresh()

    def refresh(self,*_):
        hidden=0;count=0;total=0;cur={d['E']:d for d in self.current['rows']}
        for i,(d,(check,qty)) in enumerate(zip(self.rows,self.widgets)):
            scope=self.scope.currentText();visible=(not self.search.text() or self.search.text().lower() in (d['E']+' '+str(d.get('D',''))).lower())
            visible &= self.warehouse.currentText() in ('全部仓',str(d.get('AU','')))
            visible &= self.category.currentText() in ('全部分类',str(d.get('_category') or '未分类'))
            visible &= scope=='全部新品' or scope=='可选' and check.isEnabled() or scope=='必卖' and bool(d.get('_must_sell')) or scope=='已选' and check.isChecked() or scope=='未获预算' and bool(cur[d['E']].get('_budget_gap',0))
            self.table.setRowHidden(i,not visible)
            cost=qty.value()*d['V'] if sem.numeric(d.get('V')) else None;self.table.item(i,9).setText(sem.fmt(cost))
            if check.isChecked():count+=1;hidden+=not visible;total+=cost or 0
        self.note.setText(f'全店已选 {count} 款 · 首铺候选 ¥{total:,.2f} · 筛选外已选 {hidden} 款。新品与动销共同分配总预算；必卖新品优先于普通动销。')

    def bulk(self,on):
        indices=[i for i,(c,q) in enumerate(self.widgets) if c.isEnabled() and not self.table.isRowHidden(i)]
        money=sum(self.widgets[i][1].value()*self.rows[i]['V'] for i in indices)
        if QMessageBox.question(self,'批量选择',f"对当前筛选 {len(indices)} 款执行{'选中' if on else '清空'}？候选金额 ¥{money:,.2f}",QMessageBox.StandardButton.Ok|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)!=QMessageBox.StandardButton.Ok:return
        # Freeze indices: checking a row can itself change the visible filter.
        for i in indices:self.widgets[i][0].setChecked(on)
        self.refresh()

    def clear_all(self):
        if QMessageBox.question(self,'清空全店', '清空全部新品选择，包含筛选外商品？',QMessageBox.StandardButton.Ok|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)!=QMessageBox.StandardButton.Ok:return
        for c,q in self.widgets:c.setChecked(False)

    def selections(self):return {d['E']:q.value() for d,(c,q) in zip(self.rows,self.widgets) if c.isChecked()}
