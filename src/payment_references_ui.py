"""A single-field editor inside the selected order's details."""
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLineEdit
from ui_controls import label, button
from business_ui import grid, run
from runtime_support import Cancelled
import payment_references as service


class ReferenceDialog(QDialog):
    def __init__(self, parent, host, head):
        super().__init__(parent); self.host=host; self.path=host.work_repository.path; self.receipt=None
        self.setWindowTitle('维护聚水潭内部订单号'); self.resize(720,330)
        self.current=run(self,service.read,(self.path,head),'读取内部订单号')
        v=QVBoxLayout(self)
        v.addWidget(label(head.get('name',head['store'])+' · '+head['warehouse'],'section'))
        v.addWidget(label('补货单号：'+head['number']))
        v.addWidget(label('仅保存本机订单号，不改变付款数量、金额和已有采购关联。','muted'))
        v.addWidget(label('聚水潭内部订单号'))
        self.number=QLineEdit(self.current['number']); self.number.setObjectName('jstInternalOrderNumber'); self.number.setMaxLength(100); self.number.setPlaceholderText('填写或粘贴聚水潭内部订单号'); v.addWidget(self.number)
        self.note=label('当前：'+(self.current['number'] or '未填写')+' · '+self.current['source']); v.addWidget(self.note)
        row=QHBoxLayout(); self.history_btn=button('查看修改记录',self.show_history); self.history_btn.setEnabled(bool(self.current['history'])); row.addWidget(self.history_btn); row.addStretch(); row.addWidget(button('取消',self.reject)); self.save_btn=button('保存',self.save,True); row.addWidget(self.save_btn); v.addLayout(row)
        self.number.setFocus(); self.number.selectAll()

    def save(self):
        try:
            self.receipt=run(self,service.save,(self.path,self.current,self.number.text()),'保存内部订单号')
            self.accept()
        except Cancelled:self.note.setText('已取消，未保存。')
        except Exception as exc:self.note.setText(str(exc))

    def show_history(self):
        d=QDialog(self); d.setWindowTitle('内部订单号修改记录'); d.resize(900,500); v=QVBoxLayout(d)
        table,model=grid(d,['修改时间','原内部订单号','新内部订单号'],self.current['history']); v.addWidget(table,1); v.addWidget(button('关闭',d.accept)); d.exec(); d.deleteLater()


def edit_reference(parent,host,head):
    from PySide6.QtWidgets import QMessageBox
    try:
        dialog=ReferenceDialog(parent,host,head)
        try:
            dialog.exec()
            return dialog.receipt
        finally:dialog.deleteLater()
    except Cancelled:return None
    except Exception as exc:QMessageBox.warning(parent,'内部订单号未保存',str(exc));return None
