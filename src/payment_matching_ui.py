"""Explicit maintenance of confirmed store aliases, never order re-binding."""
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QTableWidget,QTableWidgetItem,QMessageBox
from ui_controls import button,label

def maintain_aliases(parent,path):
    from payment_matching import aliases,accounts,forget_alias
    dialog=QDialog(parent);dialog.setWindowTitle('已确认付款来源门店对应');dialog.resize(1000,600)
    layout=QVBoxLayout(dialog);layout.addWidget(label('解除后，新来源单需重新确认门店；已有内部订单号的原关联、付款和签发保持。','muted'))
    table=QTableWidget(0,3);table.setHorizontalHeaderLabels(['来源企业','原表门店名称','本机门店编码']);layout.addWidget(table,1)
    note=label('');layout.addWidget(note);changed=False;values=[]
    def refresh():
        nonlocal values
        values=aliases(path);companies={a['coid']:a['name'] for a in accounts(path)['choices']};table.setRowCount(len(values))
        for i,row in enumerate(values):
            v=row['value']
            for j,text in enumerate((companies.get(v['source_scope'],'原企业 '+v['source_scope']),v['original_name'],v['store'])):table.setItem(i,j,QTableWidgetItem(text))
        table.resizeColumnsToContents()
    def remove():
        nonlocal changed
        i=table.currentRow()
        if i<0:note.setText('先选中需要解除的一条门店对应。');return
        v=values[i]
        if QMessageBox.question(dialog,'解除所选门店对应','解除“'+v['value']['original_name']+'”的门店名称对应？\n将先备份该条对应，不删除任何订单事实或内部订单号关联。')!=QMessageBox.StandardButton.Yes:return
        try:
            backup=forget_alias(path,v['key'],v['token']);changed=True;refresh();note.setText('所选门店对应已解除；备份：'+backup)
        except Exception as exc:note.setText('未解除：'+str(exc))
    row=QHBoxLayout();row.addWidget(button('解除所选门店名称对应',remove));row.addWidget(button('关闭',dialog.accept));layout.addLayout(row)
    from table_readability import install
    install(table);table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    refresh();dialog.exec();dialog.deleteLater();return changed
