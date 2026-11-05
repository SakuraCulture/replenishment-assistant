"""Small batch choices, using the existing patch preview/commit contract."""
from PySide6.QtWidgets import QDialog, QVBoxLayout, QFormLayout, QHBoxLayout, QComboBox, QLineEdit, QMessageBox
from ui_controls import label, button
import restock_policy
import store_parameter_ui as imports


class QuickSettings(QDialog):
    def __init__(self, parent, count, scope):
        super().__init__(parent)
        self.setObjectName('parameterQuickSettings')
        self.setWindowTitle('批量设置开关、预算与供货仓')
        self.resize(660, 460)
        v = QVBoxLayout(self)
        v.addWidget(label(f'已选 {count} 家门店 · '+('保存以后默认' if scope == 'defaults' else '仅应用本次作业'), 'section'))
        v.addWidget(label('未选择的项目保持各店原值；无需填写Excel。', 'muted'))
        form = QFormLayout(); self.fields = {}
        for name in ['启用一箱例外', *restock_policy.LABELS.values()]:
            box = QComboBox(); box.addItem('保持各店原值', None)
            for text, value in [('开启', '是' if name == '启用一箱例外' else '开启'), ('关闭', '否' if name == '启用一箱例外' else '关闭')]:box.addItem(text, value)
            form.addRow(name, box); self.fields[name] = box
        self.route = QComboBox()
        for text, value in [('保持各店原值', None), ('山东仓单仓', ('山东仓', '无从仓')), ('河北仓单仓', ('河北仓', '无从仓')), ('双仓：山东主仓、河北从仓', ('山东仓','河北仓')), ('双仓：河北主仓、山东从仓', ('河北仓','山东仓'))]:self.route.addItem(text,value)
        form.addRow('供货方式', self.route)
        self.budget = QComboBox()
        for text,value in [('保持各店原值',None),('不限预算','不限预算'),('限定预算','限定预算')]:self.budget.addItem(text,value)
        self.amount = QLineEdit(); self.amount.setPlaceholderText('输入统一金额；0元表示不安排'); self.amount.setEnabled(False)
        self.budget.currentIndexChanged.connect(lambda _:self.amount.setEnabled(self.budget.currentData()=='限定预算'))
        form.addRow('预算方式', self.budget); form.addRow('每店预算（元）', self.amount); v.addLayout(form)
        note=label('供货方式只改主从仓，保留各仓周期。开启新品铺设仍需选品；应用到本次后，变化门店需要重算。','muted');note.setWordWrap(True);v.addWidget(note)
        self.error=label('','muted');v.addWidget(self.error)
        row=QHBoxLayout();cancel=button('取消',self.reject);cancel.setDefault(True);row.addWidget(cancel);row.addWidget(button('核对变化',self.finish,True));v.addLayout(row)

    def values(self):
        values={(name,):box.currentData() for name,box in self.fields.items() if box.currentData() is not None}
        if self.route.currentData():
            primary,secondary=self.route.currentData();values.update({('主仓',):primary,('从仓',):secondary})
        if self.budget.currentData():values[('预算方式',)]=self.budget.currentData()
        if self.budget.currentData()=='限定预算':
            from store_parameter_import import number
            if not self.amount.text().strip():raise ValueError('限定预算需要填写金额；如不修改预算，请选“保持各店原值”。')
            values[('预算金额',)]=number(self.amount.text())
        return values

    def finish(self):
        try:
            if not self.values():raise ValueError('尚未选择要修改的项目。')
        except ValueError as exc:self.error.setText(str(exc));return
        self.accept()


def open_quick(parent,host,codes,scope='defaults'):
    if not codes:
        QMessageBox.information(parent,'请选择门店','请先勾选需要批量设置的门店。');return
    if scope!='defaults' and (host.readonly or host.worker is not None):
        QMessageBox.information(parent,'当前不能应用','请结束当前计算；历史作业只读。');return
    d=QuickSettings(parent,len(codes),scope)
    if d.exec()!=QDialog.DialogCode.Accepted:return
    try:
        data={'sources':{},'scoped':True,'formats':['批量设置：未选择的项目保持各店原值'],
              'rows':[{'code':s,'name':'','values':d.values(),'source':'界面批量设置'} for s in codes]}
        snapshot=imports.capture(host,data);check=imports.Preview(parent,host,data,snapshot,scope)
        check.setWindowTitle('核对批量设置');check.scope.setEnabled(False)
        if check.exec()==QDialog.DialogCode.Accepted:
            imports.commit(host,data,snapshot,scope,check.prepared,
                persist=lambda values,expected:imports.run(parent,imports.save_values,(host.work_repository,values,expected),'保存门店设置',cancellable=False))
    except Exception as exc:QMessageBox.warning(parent,'未应用设置',str(exc))
