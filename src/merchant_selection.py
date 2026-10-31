"""Explicit store/warehouse selection, independent of the current detail row."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QTableWidget,QTableWidgetItem,QDialogButtonBox,QPushButton,QLabel,QLineEdit,QFileDialog,QMessageBox
import warehouse_orders

class MerchantSelection(QDialog):
    def __init__(self,parent,order=None):
        super().__init__(parent);self.setObjectName('merchantSelection');self.setWindowTitle('选择门店与仓 · 加盟商确认单');self.resize(1060,520)
        layout=QVBoxLayout(self);layout.addWidget(QLabel('每个门店每仓一份文件。默认全选；只导出所选仓，但签发仍保存该门店完整正常单与同一修订。'))
        self.table=QTableWidget(0,8);self.table.setHorizontalHeaderLabels(['导出','门店名称','门店编码','供货仓','状态','动销款数','新品款数','安排金额']);self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);layout.addWidget(self.table);self.items=[]
        states=parent.states if order is None else {order['store']:{'state':'已完成','name':order['payload']['result']['store_name']}}
        results=parent.results if order is None else {order['store']:order['payload']['result']}
        profile=getattr(parent,'new_focus',None) if order is None else order['payload'].get('new_focus')
        for s,state in states.items():
            valid=state['state']=='已完成' and s in results;error=state['state'];summaries=[]
            if valid:
                try:summaries=warehouse_orders.summaries(results[s],profile)
                except ValueError as exc:error=str(exc);valid=False
            if not summaries:summaries=[{'warehouse':None,'moving':0,'new':0,'amount':0}];valid=False;error=error if error!='已完成' else '没有可导出商品'
            for info in summaries:
                i=self.table.rowCount();self.table.insertRow(i);check=QTableWidgetItem();check.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsUserCheckable if valid else Qt.ItemFlag.NoItemFlags);check.setCheckState(Qt.CheckState.Checked if valid else Qt.CheckState.Unchecked);self.table.setItem(i,0,check);self.items.append((s,info['warehouse'],check,valid))
                for j,v in enumerate([state['name'],s,info['warehouse'] or '—','可导出' if valid else error,info['moving'],info['new'],f"{info['amount']:,.2f}"],1):self.table.setItem(i,j,QTableWidgetItem(str(v)))
        self.table.resizeColumnsToContents();row=QHBoxLayout()
        for title,value in [('全选有效仓',True),('取消全选',False)]:
            button=QPushButton(title);button.clicked.connect(lambda _,v=value:self.select_all(v));row.addWidget(button)
        self.note=QLabel();row.addWidget(self.note);layout.addLayout(row)
        self.destination=None
        if order is None:
            import export_locations
            self.destination=QLineEdit(export_locations.initial_directory(parent));row=QHBoxLayout();row.addWidget(QLabel('本次导出位置'));row.addWidget(self.destination,1);browse=QPushButton('更改本次目录');browse.clicked.connect(self.browse);row.addWidget(browse);layout.addLayout(row)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel);self.ok=buttons.button(QDialogButtonBox.StandardButton.Ok);self.ok.setText('下一步：核对签发与输出范围');self.ok.setAutoDefault(False);self.ok.setDefault(False);buttons.button(QDialogButtonBox.StandardButton.Cancel).setDefault(True);buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消');buttons.accepted.connect(self.accept);buttons.rejected.connect(self.reject);layout.addWidget(buttons);self.table.itemChanged.connect(self.update_count);self.update_count()
    def warehouses(self):
        values={}
        for s,w,c,valid in self.items:
            if valid and c.checkState()==Qt.CheckState.Checked:values.setdefault(s,[]).append(w)
        return values
    def browse(self):
        path=QFileDialog.getExistingDirectory(self,'选择本次导出目录',self.destination.text())
        if path:self.destination.setText(path)
    def accept(self):
        if self.destination is not None:
            import export_settings
            try:self.destination.setText(export_settings.validate(self.destination.text(),writable=True))
            except Exception as exc:QMessageBox.warning(self,'请选择可用目录',str(exc));return
            if not self.destination.text():return
        super().accept()
    def codes(self):return list(self.warehouses())
    def select_all(self,value):
        for _,_,c,valid in self.items:
            if valid:c.setCheckState(Qt.CheckState.Checked if value else Qt.CheckState.Unchecked)
    def update_count(self,*_):
        selected=self.warehouses();count=sum(map(len,selected.values()));self.note.setText(f'已选 {len(selected)} 家门店，{count} 份仓确认单');self.ok.setEnabled(count>0)
