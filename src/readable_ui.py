"""Shared business tables and expandable technical evidence; display only."""
from pathlib import Path
from PySide6.QtCore import Qt,QUrl,QTimer,QObject,QEvent
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLineEdit,QMessageBox,
    QDialogButtonBox,QLayout,QHeaderView,QPlainTextEdit,QCheckBox)
from business_ui import grid
from ui_controls import label,button
from business_presenter import value_text,path_title,file_name


class ReviewDialog(QDialog):
    """Resizable review with the confirmation roles used by existing callers."""
    def __init__(self,parent=None):
        super().__init__(parent);self._clicked=None;self._escape=None
        layout=QVBoxLayout(self);self.description=QPlainTextEdit();self.description.setReadOnly(True);self.description.setMaximumHeight(130);layout.addWidget(self.description)
        self.information=label('');layout.addWidget(self.information);self.body=QVBoxLayout();layout.addLayout(self.body,1)
        self.button_box=QDialogButtonBox();layout.addWidget(self.button_box);self.button_box.clicked.connect(self.choose)
        from table_readability import fit_window
        fit_window(self,1180,760)
    def setText(self,text):self.description.setPlainText(text)
    def text(self):return self.description.toPlainText()
    def setInformativeText(self,text):self.information.setText(text)
    def setDetailedText(self,text):
        self._detail=text
        if not hasattr(self,'detail_view'):
            self.detail_view=QPlainTextEdit();self.detail_view.setReadOnly(True);self.detail_view.setMaximumHeight(150);self.detail_view.hide()
            toggle=QCheckBox('显示技术详情（可复制）');toggle.toggled.connect(self.detail_view.setVisible)
            self.layout().insertWidget(self.layout().count()-1,toggle);self.layout().insertWidget(self.layout().count()-1,self.detail_view)
        self.detail_view.setPlainText(text)
    def detailedText(self):return getattr(self,'_detail','')
    def setIcon(self,icon):pass
    def addButton(self,text,role):return self.button_box.addButton(text,QDialogButtonBox.ButtonRole(role.value))
    def setDefaultButton(self,button):self._default=button;button.setDefault(True);button.setAutoDefault(False)
    def defaultButton(self):return getattr(self,'_default',None)
    def escapeButton(self):return self._escape
    def buttons(self):return self.button_box.buttons()
    def setEscapeButton(self,button):self._escape=button
    def clickedButton(self):return self._clicked
    def buttonRole(self,button):return QMessageBox.ButtonRole(self.button_box.buttonRole(button).value)
    def choose(self,button):self._clicked=button;self.done(QDialog.DialogCode.Accepted if self.buttonRole(button)!=QMessageBox.ButtonRole.RejectRole else QDialog.DialogCode.Rejected)
    def reject(self):self._clicked=self._escape;super().reject()
    def exec(self):self._clicked=None;return super().exec()


def details(parent,title,headers,rows,note=''):
    dialog=QDialog(parent);dialog.setWindowTitle(title);layout=QVBoxLayout(dialog)
    if note:layout.addWidget(label(note,'muted'))
    search=QLineEdit();search.setPlaceholderText('搜索全部已载入内容');search.setClearButtonEnabled(True);layout.addWidget(search)
    table,model=grid(dialog,headers,rows);layout.addWidget(table,1)
    body=QPlainTextEdit();body.setReadOnly(True);body.setPlaceholderText('选择一行查看全部字段，可选择和复制完整文本');body.setMaximumHeight(220);layout.addWidget(body)
    def selection(*_):
        from table_readability import row_values
        hs,values=row_values(table);body.setPlainText('\n\n'.join(f'{h}：\n{v if v is not None else ""}' for h,v in zip(hs,values)))
    table.selectionModel().currentChanged.connect(selection);search.textChanged.connect(model.filter);model.modelReset.connect(body.clear)
    layout.addWidget(button('关闭',dialog.accept))
    from table_readability import fit_window
    fit_window(dialog)
    if model.rowCount():table.setCurrentIndex(model.index(0,0))
    dialog.exec()

class ReviewLayout(QObject):
    """QMessageBox rebuilds its private layout when text/buttons change."""
    def __init__(self,box,widgets):
        # Parent owns this observer; do not retain the parent/private layout in
        # Python cycles that can be collected during another modal event loop.
        super().__init__(box);self.widgets=widgets;self.pending=False
        box.installEventFilter(self);self.schedule()
    def schedule(self):
        if not self.pending:
            self.pending=True;QTimer.singleShot(0,self,self.attach)
    def eventFilter(self,obj,event):
        if event.type()==QEvent.Type.Show:self.schedule()
        elif event.type()==QEvent.Type.LayoutRequest:
            layout=obj.layout()
            if layout is not None and any(layout.indexOf(w)<0 for w in self.widgets):self.schedule()
        return False
    def attach(self):
        from shiboken6 import isValid
        self.pending=False
        box=self.parent()
        if box is None or not isValid(box) or not all(isValid(w) for w in self.widgets):return
        layout=box.layout()
        if layout is None:return
        buttons=box.findChild(QDialogButtonBox)
        for widget in (*self.widgets,buttons):
            if widget is not None:layout.removeWidget(widget)
        at=max((layout.getItemPosition(i)[0]+layout.getItemPosition(i)[2] for i in range(layout.count())),default=0)
        span=max(1,layout.columnCount())
        for n,widget in enumerate(self.widgets):layout.addWidget(widget,at+n,0,1,span)
        if buttons:layout.addWidget(buttons,at+len(self.widgets),0,1,span)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)


def review_table(box,headers,rows,*,total=None):
    """Keep existing QMessageBox confirmation roles/default/escape behavior."""
    rows=list(rows);box.setDetailedText('')
    search=QLineEdit(box);search.setPlaceholderText('搜索门店 / 商品 / 参数 / 变化项');search.setClearButtonEnabled(True)
    table,model=grid(box,headers,rows);table.setObjectName('businessReviewTable');table.setMinimumSize(300,150)
    table.setWordWrap(False)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    for i in range(len(headers)):table.setColumnWidth(i,180 if i<2 else 150)
    caption=label(f'已载入 {len(rows)} 项'+(f' / 共 {total} 项；此处截断，请用原有完整差异导出查看全部。' if total and total>len(rows) else '；仅筛选显示，不改变确认范围。'),'muted')
    caption.setParent(box);search.textChanged.connect(model.filter)
    detail=button('查看当前行完整内容（Ctrl+Shift+D）',lambda:__import__('table_readability').full_row(table))
    box.business_table=table;box.business_model=model;box.business_search=search
    if isinstance(box,ReviewDialog):
        box.body.addWidget(caption);box.body.addWidget(search);box.body.addWidget(table,1);box.body.addWidget(detail)
    else:
        box.business_layout=ReviewLayout(box,(caption,search,table,detail))
        from table_readability import fit_window
        fit_window(box,1100,720)
    return table,model

def technical(parent,text,caption='查看技术详情'):
    toggle=QCheckBox(caption);body=QPlainTextEdit();body.setReadOnly(True);body.setPlainText(str(text));body.setMaximumHeight(180);body.hide();toggle.toggled.connect(body.setVisible)
    return toggle,body

def file_results(parent,paths,title='导出完成',note=''):
    paths=list(paths);d=QDialog(parent);d.setWindowTitle(title);d.resize(1000,600);v=QVBoxLayout(d)
    v.addWidget(label(f'已生成 {len(paths)} 个文件','section'))
    if note:v.addWidget(label(note,'muted'))
    rows=[[file_name(p),'已生成'] for p in paths];table,model=grid(d,['文件名（含门店 / 仓 / 用途）','结果'],rows);table.setColumnWidth(0,720);v.addWidget(table,1)
    location=label('选择一项可打开文件或所在目录','muted');v.addWidget(location)
    def selected():
        i=table.currentIndex().row()
        return Path(paths[model.indices[i]]) if i>=0 else None
    def open_item(folder=False):
        p=selected()
        if p:QDesktopServices.openUrl(QUrl.fromLocalFile(str(p.parent if folder else p)))
    table.clicked.connect(lambda _:location.setText(str(selected())))
    row=QHBoxLayout();row.addWidget(button('打开文件',lambda:open_item()));row.addWidget(button('打开所在目录',lambda:open_item(True)));row.addWidget(button('关闭',d.accept));v.addLayout(row)
    if paths:table.selectRow(0)
    d.exec()

def failure_message(exc):
    raw=str(exc)
    if '导出任务与恢复' in raw and '已生成 ' in raw:
        import re
        amount=re.search(r'已生成 \d+/\d+ 份',raw)
        reason='文件已生成，导出日志未写完。' if '日志写入失败' in raw else '部分文件写出未完成；请关闭占用文件或更换可写目录。'
        return raw.splitlines()[0]+'\n'+(amount.group()+'。' if amount else '')+reason+'\n打开“显示与帮助 → 导出任务与恢复”，按原修订补导出；不要重新签发。'
    if 'UNRESOLVED_TASK' in raw or 'SUBMISSION_UNKNOWN' in raw:
        message='平台是否已受理尚未核实。请核对原任务，不要重复提交。'
    elif 'locked' in raw.lower():message='数据暂被其他操作占用。请等待当前操作结束，再重试。'
    elif isinstance(exc,PermissionError) or 'PermissionError' in raw:message='文件暂不能写入。请关闭占用文件的Excel，或选择有写入权限的位置后重试。'
    elif 'KeyError' in raw or 'Traceback' in raw:message='处理数据时遇到异常，当前操作未完成。请复制技术详情用于排查。'
    else:message=raw if len(raw)<180 and not ('\\' in raw or ' / ' in raw) else '当前操作未完成。请查看下方原因并核对输入或任务状态后再处理。'
    return message


def failure(parent,title,exc):
    box=QMessageBox(parent);box.setIcon(QMessageBox.Icon.Warning);box.setWindowTitle(title);box.setText(failure_message(exc));box.setDetailedText(str(exc));box.setStandardButtons(QMessageBox.StandardButton.Ok);box.exec()
