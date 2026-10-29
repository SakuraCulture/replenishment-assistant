"""Explicit store selection for merchant exports, independent of current detail row."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QTableWidget,QTableWidgetItem,QDialogButtonBox,QPushButton,QLabel


class MerchantSelection(QDialog):
    def __init__(self,parent):
        super().__init__(parent);self.setObjectName('merchantSelection');self.setWindowTitle('选择导出门店 · 加盟商确认单');self.resize(820,470)
        layout=QVBoxLayout(self);layout.addWidget(QLabel('每店生成一份文件，仅包含中心仓商品。默认勾选全部有效完成门店，可取消不需要的门店。'))
        self.table=QTableWidget(len(parent.states),5);self.table.setHorizontalHeaderLabels(['导出','门店名称','门店编码','计算状态','安排金额']);self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);layout.addWidget(self.table);self.items=[]
        for i,(s,state) in enumerate(parent.states.items()):
            valid=state['state']=='已完成' and s in parent.results
            check=QTableWidgetItem();check.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsUserCheckable if valid else Qt.ItemFlag.NoItemFlags);check.setCheckState(Qt.CheckState.Checked if valid else Qt.CheckState.Unchecked);self.table.setItem(i,0,check);self.items.append((s,check,valid))
            amount=f"{parent.results[s]['summary']['amount']:,.2f}" if valid else '—'
            for j,v in enumerate([state['name'],s,state['state'],amount],1):self.table.setItem(i,j,QTableWidgetItem(str(v)))
        self.table.resizeColumnsToContents();row=QHBoxLayout()
        for title,value in [('全选有效门店',True),('取消全选',False)]:
            button=QPushButton(title);button.clicked.connect(lambda _,v=value:self.select_all(v));row.addWidget(button)
        self.note=QLabel();row.addWidget(self.note);layout.addLayout(row)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel);self.ok=buttons.button(QDialogButtonBox.StandardButton.Ok);self.ok.setText('下一步：核对并导出');buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消');buttons.accepted.connect(self.accept);buttons.rejected.connect(self.reject);layout.addWidget(buttons);self.table.itemChanged.connect(self.update_count);self.update_count()

    def codes(self):return [s for s,c,valid in self.items if valid and c.checkState()==Qt.CheckState.Checked]
    def select_all(self,value):
        for _,c,valid in self.items:
            if valid:c.setCheckState(Qt.CheckState.Checked if value else Qt.CheckState.Unchecked)
    def update_count(self,*_):
        count=len(self.codes());self.note.setText(f'已选 {count} 家门店');self.ok.setEnabled(count>0)
