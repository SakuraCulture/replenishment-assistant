"""Scrollable result and confirmation dialogs for automatic JST orders."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLineEdit, QPlainTextEdit, QVBoxLayout)

from business_ui import grid
from table_readability import fit_window
from ui_controls import button, label


def failure_rows(failures):
    return [(r.get('warehouse','—'),r.get('number') or r['order'],r.get('online','—'),
             '制表待处理',r['reason']) for r in failures]


def _grid(parent, headers, values, widths):
    table, model = grid(parent,headers,values)
    table.reading_controller.apply()
    for i, width in enumerate(widths):
        table.setColumnWidth(i,width)
    return table, model


class ImportConfirmation(QDialog):
    def __init__(self,parent,rows):
        super().__init__(parent)
        self.setWindowTitle('确认导入')
        self.setObjectName('jstImportConfirmation')
        fit_window(self,1020,590)
        layout = QVBoxLayout(self)
        layout.addWidget(label(f'确认导入本次勾选且通过预览的 {len(rows)} 张仓单。'))
        values = [(r['warehouse'],r['number'],r['online'],r['name'] or r['store'],
                   r['sku_count'],r['quantity'],r['preview_amount'] if r['preview_amount'] is not None else '待核')
                  for r in rows]
        self.table, self.model = _grid(self,('仓','补货单号','线上单号','门店','款数','数量','平台预览金额'),
                                      values,(76,188,222,135,62,70,114))
        layout.addWidget(self.table,1)
        footer = QHBoxLayout()
        self.cancel_btn = button('取消',self.reject)
        self.cancel_btn.setDefault(True)
        self.confirm_btn = button('确认导入',self.accept,True)
        footer.addStretch(1)
        footer.addWidget(self.cancel_btn)
        footer.addWidget(self.confirm_btn)
        layout.addLayout(footer)


class JobDetails(QDialog):
    HEADERS = ('仓','补货单号','线上单号','结果','处理原因')

    def __init__(self,parent,rows):
        super().__init__(parent)
        self.setWindowTitle('聚水潭自动下单 · 作业明细')
        fit_window(self,1070,660)
        layout = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText('查找仓、单号、结果或原因')
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName('查找作业明细')
        layout.addWidget(self.search)
        self.table, self.model = _grid(self,self.HEADERS,rows,(76,188,220,156,320))
        layout.addWidget(self.table,3)
        self.reason = QPlainTextEdit()
        self.reason.setReadOnly(True)
        self.reason.setPlaceholderText('选择一条作业记录查看完整原因')
        self.reason.setAccessibleName('完整处理原因')
        self.reason.setMinimumHeight(72)
        layout.addWidget(self.reason,1)
        footer = QHBoxLayout()
        self.copy_btn = button('复制所选记录',self.copy_record)
        self.close_btn = button('关闭',self.reject)
        self.close_btn.setText('关闭')
        footer.addStretch(1)
        footer.addWidget(self.copy_btn)
        footer.addWidget(self.close_btn)
        layout.addLayout(footer)
        self.table.selectionModel().currentRowChanged.connect(self.show_reason)
        self.search.textChanged.connect(self.filter)
        self.filter('')

    def filter(self,text):
        self.model.filter(text)
        self.reason.clear()
        self.copy_btn.setEnabled(False)
        if self.model.rowCount():
            self.table.selectRow(0)

    def show_reason(self,index,_previous):
        if index.isValid():
            self.reason.setPlainText(self.model.data(self.model.index(index.row(),4)))
        else:
            self.reason.clear()
        self.copy_btn.setEnabled(index.isValid())

    def copy_record(self):
        current = self.table.currentIndex()
        if not current.isValid():
            return
        values = [self.model.data(self.model.index(current.row(),i)) for i in range(len(self.HEADERS))]
        text = '\n'.join(f'{header}\t{value}' for header,value in zip(self.HEADERS,values))
        QApplication.clipboard().setText(text)


def confirm_import(parent,rows):
    dialog = ImportConfirmation(parent,rows)
    try:
        return dialog.exec()==QDialog.DialogCode.Accepted
    finally:
        dialog.deleteLater()


def show_job_details(parent,rows):
    dialog = JobDetails(parent,rows)
    try:
        dialog.exec()
    finally:
        dialog.deleteLater()
