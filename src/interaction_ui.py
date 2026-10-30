"""Explicit UI scopes and confirmations; no model or persistence policy here."""
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QCheckBox,QMessageBox
from ui_controls import button,label
from business_ui import grid


def confirm(parent,title,text):
    return QMessageBox.question(parent,title,text,QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)==QMessageBox.StandardButton.Yes


def show_rows(parent,title,headers,rows,note=''):
    d=QDialog(parent);d.setWindowTitle(title);d.resize(1050,650);v=QVBoxLayout(d)
    if note:v.addWidget(label(note,'muted'))
    table,model=grid(d,headers,rows);v.addWidget(table,1);v.addWidget(button('关闭',d.reject));d.exec()


def review_rows(parent,title,headers,rows,note,accept_text):
    d=QDialog(parent);d.setObjectName('interactionReview');d.setWindowTitle(title);d.resize(1100,700);v=QVBoxLayout(d);v.addWidget(label(note))
    table,model=grid(d,headers,rows);v.addWidget(table,1);actions=QHBoxLayout();cancel=button('取消',d.reject);cancel.setDefault(True);actions.addWidget(cancel);actions.addWidget(button(accept_text,d.accept,True));v.addLayout(actions)
    return d.exec()==QDialog.DialogCode.Accepted


def choose_stores(parent,title,states,initial=()):
    d=QDialog(parent);d.setObjectName('storeScopeSelection');d.setWindowTitle(title);d.resize(650,520);v=QVBoxLayout(d);v.addWidget(label('仅勾选的有效完成门店参与；未完成或过期门店不可选。'));checks={}
    from PySide6.QtWidgets import QScrollArea,QWidget
    scroll=QScrollArea();content=QWidget();body=QVBoxLayout(content)
    for code,state in states.items():
        c=QCheckBox(f"{state['name']}（{code}） · {state['state']}");c.setEnabled(state['state']=='已完成');c.setChecked(c.isEnabled() and code in initial);body.addWidget(c);checks[code]=c
    scroll.setWidget(content);scroll.setWidgetResizable(True);v.addWidget(scroll,1);note=label('');v.addWidget(note);row=QHBoxLayout();cancel=button('取消',d.reject);cancel.setDefault(True);row.addWidget(cancel);go=button('下一步：核对归档',d.accept,True);row.addWidget(go);v.addLayout(row)
    def update():
        n=sum(c.isChecked() for c in checks.values());note.setText(f'已选 {n} 家门店；高亮行不改变此范围');go.setEnabled(n>0)
    for c in checks.values():c.toggled.connect(update)
    update()
    return [s for s,c in checks.items() if c.isChecked()] if d.exec()==QDialog.DialogCode.Accepted else []
