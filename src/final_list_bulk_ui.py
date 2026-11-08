"""Batch edits are previewed completely before replacing the draft overlay."""
import copy
from decimal import Decimal
from PySide6.QtCore import Qt,QAbstractTableModel,QModelIndex
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QComboBox,QLineEdit,QLabel,QTableView
from ui_controls import button
import final_list_filters as rules
import preissue
from restock_policy import guard_new
import work_repository as wr
import result_semantics as sem

MODES={'add':'增加N个最小出库单位','subtract':'减少N个最小出库单位（最低0）','packs':'每款设为N个最小出库单位','quantity':'设置相同库存单位数量','zero':'清零'}


def plan(model,indices,mode,amount,reason):
    reason=reason.strip()
    if not 1<=len(reason)<=200:raise ValueError('请填写1～200字调整原因')
    if not indices:raise ValueError('请先勾选商品')
    if model.invalid:raise ValueError('仍有非法数量，请先修正或恢复')
    updates=copy.deepcopy(model.edits);rows=[];sequence=model.sequence;delta=Decimal(0)
    for i in indices:
        s,d=model.entries[i]
        if model.blocks[i]:raise ValueError(d['E']+'：'+model.blocks[i])
        before=model.qty(i);q=rules.adjusted_quantity(d,before,mode,amount)
        guard_new(model.results[s],d,'manual',q)
        ref=updates[s].get(d['E'],{}).get('list_revision')
        if model.lists[s].get(ref,{}).get('kind')=='new':guard_new(model.results[s],d,'new',q)
        change=(preissue.decimal(q)-preissue.decimal(before))*preissue.decimal(d['V'])
        delta+=change
        rows.append([model.results[s]['store_name']+' / '+s,d['AU'],d['E'],d.get('D',''),before,q,q-before,float(change)])
        if q==before:continue
        updates[s][d['E']]={**updates[s].get(d['E'],{}),'quantity':q,'kind':'manual','reason':reason,'warehouse':d['AU'],'sequence':sequence,'edited_at':wr.now()};sequence+=1
    return {'edits':updates,'rows':rows,'sequence':sequence,'amount_delta':str(delta),'changed':sum(r[4]!=r[5] for r in rows),'stores':len({model.entries[i][0] for i in indices})}


class PreviewModel(QAbstractTableModel):
    headers=['门店名称 / 编码','仓','商品编码','商品名称','调整前','调整后','数量变化','金额变化（元）']
    def __init__(self,rows,parent=None):super().__init__(parent);self.rows=rows
    def rowCount(self,parent=QModelIndex()):return 0 if parent.isValid() else len(self.rows)
    def columnCount(self,parent=QModelIndex()):return 0 if parent.isValid() else len(self.headers)
    def headerData(self,i,orientation,role=Qt.ItemDataRole.DisplayRole):
        if role==Qt.ItemDataRole.DisplayRole:return self.headers[i] if orientation==Qt.Orientation.Horizontal else i+1
    def data(self,index,role=Qt.ItemDataRole.DisplayRole):
        if index.isValid() and role in (Qt.ItemDataRole.DisplayRole,Qt.ItemDataRole.ToolTipRole):return sem.fmt(self.rows[index.row()][index.column()])


class BulkDialog(QDialog):
    def __init__(self,model,indices,scope,parent=None):
        super().__init__(parent);self.setWindowTitle('批量加减 / 设数量 · 先预览再填入');self.resize(1150,620)
        self.source=model;self.indices=indices;self.prepared=None;v=QVBoxLayout(self)
        self.scope_label=QLabel(f'范围：{scope}，共 {len(indices)} 款。以当前拟签发数量为基准；数量按库存单位，每款按本仓箱规。');self.scope_label.setWordWrap(True);v.addWidget(self.scope_label)
        row=QHBoxLayout();self.mode=QComboBox()
        for code,title in MODES.items():self.mode.addItem(title,code)
        self.amount=QLineEdit('1');self.reason=QLineEdit(model.reason);self.reason.setPlaceholderText('本次统一调整原因（必填，写入实际变化行）');self.reason.setMaxLength(200)
        row.addWidget(self.mode);row.addWidget(self.amount);row.addWidget(self.reason,1);row.addWidget(button('预览全部变化',self.preview));v.addLayout(row)
        self.table=QTableView();self.table.setAlternatingRowColors(True);v.addWidget(self.table,1)
        for widget in (self.amount,self.reason):widget.textChanged.connect(self.invalidate)
        self.mode.currentIndexChanged.connect(self.invalidate)
        self.note=QLabel('尚未预览；预览和取消均不会改写当前清单。');self.note.setWordWrap(True);v.addWidget(self.note)
        actions=QHBoxLayout();actions.addStretch();actions.addWidget(button('取消',self.reject));self.fill=button('确认填入当前清单',self.accept,True);self.fill.setEnabled(False);actions.addWidget(self.fill);v.addLayout(actions)
        from table_readability import install,fit_window
        install(self.table);fit_window(self,1150,620)
    def invalidate(self,*_):
        self.prepared=None;self.fill.setEnabled(False);self.table.setModel(None);self.note.setText('条件已变化，请重新预览。');self.amount.setEnabled(self.mode.currentData()!='zero')
    def preview(self):
        self.invalidate()
        try:p=plan(self.source,self.indices,self.mode.currentData(),'0' if self.mode.currentData()=='zero' else self.amount.text(),self.reason.text())
        except ValueError as e:self.note.setText('未填入：'+str(e));return
        self.prepared=p;self.table.setModel(PreviewModel(p['rows'],self.table))
        for i,w in enumerate([240,80,170,240,80,80,90,130]):self.table.setColumnWidth(i,w)
        self.note.setText(f'涉及 {p["stores"]} 家门店 / {len(p["rows"])} 款，实际变化 {p["changed"]} 款，金额变化 {Decimal(p["amount_delta"]):+,.2f} 元。可滚动核对全部行。填入后还须核对修改与预算并应用。');self.fill.setEnabled(p['changed']>0)
    def accept(self):
        if self.prepared is not None:super().accept()
