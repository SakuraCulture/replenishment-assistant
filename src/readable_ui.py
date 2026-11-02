"""Shared business tables and expandable technical evidence; display only."""
from pathlib import Path
from PySide6.QtCore import Qt,QUrl,QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLineEdit,QMessageBox,
    QDialogButtonBox,QLayout,QHeaderView,QPlainTextEdit,QCheckBox)
from business_ui import grid
from ui_controls import label,button
from business_presenter import value_text,path_title,file_name

def review_table(box,headers,rows,*,total=None):
    """Keep existing QMessageBox confirmation roles/default/escape behavior."""
    rows=list(rows);box.setDetailedText('');layout=box.layout()
    buttons=box.findChild(QDialogButtonBox)
    if buttons:layout.removeWidget(buttons)
    at=layout.rowCount();span=max(1,layout.columnCount())
    search=QLineEdit();search.setPlaceholderText('搜索门店 / 商品 / 参数 / 变化项');search.setClearButtonEnabled(True)
    table,model=grid(box,headers,rows);table.setObjectName('businessReviewTable');table.setMinimumSize(760,260)
    table.setWordWrap(True);table.verticalHeader().setDefaultSectionSize(48)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    for i in range(len(headers)):table.setColumnWidth(i,180 if i<2 else 150)
    caption=label(f'已载入 {len(rows)} 项'+(f' / 共 {total} 项；此处截断，请用原有完整差异导出查看全部。' if total and total>len(rows) else '；仅筛选显示，不改变确认范围。'),'muted')
    search.textChanged.connect(model.filter)
    layout.addWidget(caption,at,0,1,span);layout.addWidget(search,at+1,0,1,span);layout.addWidget(table,at+2,0,1,span)
    if buttons:layout.addWidget(buttons,at+3,0,1,span)
    layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint);box.setMinimumWidth(850)
    box.business_table=table;box.business_model=model;box.business_search=search
    def settle():
        # Some callers add their established buttons after attaching the table.
        current=box.findChild(QDialogButtonBox)
        if current:layout.removeWidget(current);layout.addWidget(current,at+3,0,1,span)
    QTimer.singleShot(0,box,settle)
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

def failure(parent,title,exc):
    raw=str(exc)
    if 'UNRESOLVED_TASK' in raw or 'SUBMISSION_UNKNOWN' in raw:
        message='平台是否已受理尚未核实。请核对原任务，不要重复提交。'
    elif 'locked' in raw.lower():message='数据暂被其他操作占用。请等待当前操作结束，再重试。'
    elif isinstance(exc,PermissionError) or 'PermissionError' in raw:message='文件暂不能写入。请关闭占用文件的Excel，或选择有写入权限的位置后重试。'
    elif 'KeyError' in raw or 'Traceback' in raw:message='处理数据时遇到异常，当前操作未完成。请复制技术详情用于排查。'
    else:message=raw if len(raw)<180 and not ('\\' in raw or ' / ' in raw) else '当前操作未完成。请查看下方原因并核对输入或任务状态后再处理。'
    box=QMessageBox(parent);box.setIcon(QMessageBox.Icon.Warning);box.setWindowTitle(title);box.setText(message);box.setDetailedText(raw);box.setStandardButtons(QMessageBox.StandardButton.Ok);box.exec()
