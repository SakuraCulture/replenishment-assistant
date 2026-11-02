"""Shared styling and file selection controls; no application import."""
from pathlib import Path
from PySide6.QtCore import Qt,Signal,QSize
from PySide6.QtWidgets import QFrame,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QFileDialog,QListWidget,QAbstractItemView,QStyle

STYLE='''
QWidget {font-family:"Microsoft YaHei UI";font-size:10pt;color:#22383C;}
QMainWindow,QDialog {background:#F3F6F7;}
QFrame#card {background:white;border:1px solid #DEE6E8;border-radius:10px;}
QLabel#title {font-size:23pt;font-weight:600;color:#153C43;}
QLabel#section {font-size:15pt;font-weight:600;}
QLabel#muted {color:#647A80;}
QLabel#metric {font-size:20pt;font-weight:600;color:#12665C;}
QPushButton {background:white;border:1px solid #CCDADD;border-radius:6px;padding:8px 16px;}
QPushButton:hover {background:#EAF3F3;border-color:#78AAA4;}
QPushButton:disabled {background:#EDF1F2;color:#9CAEB2;border-color:#E2E8EA;}
QPushButton#primary {background:#176C63;color:white;border:none;font-weight:600;}
QPushButton#primary:hover {background:#12574F;}
QPushButton#primary:disabled {background:#A3C0BC;color:#F8FCFB;}
QPushButton#auxiliary {background:transparent;border:1px solid transparent;padding:5px 9px;}
QPushButton#auxiliary:hover {background:#E3EEEE;border-color:#B6CFCE;}
QPushButton#auxiliary:disabled {color:#9CAEB2;}
QLineEdit,QComboBox,QDoubleSpinBox {background:white;border:1px solid #CBDADD;border-radius:5px;padding:6px;min-height:20px;}
QCheckBox {spacing:8px;} QCheckBox::indicator {width:18px;height:18px;}
QTableView,QTableWidget {background:white;alternate-background-color:#F7FAFA;border:1px solid #DFE7E9;gridline-color:#EDF1F2;selection-background-color:#DDEFEA;selection-color:#153C43;}
QHeaderView::section {background:#EAF1F2;border:none;border-bottom:1px solid #D5E2E4;padding:9px;font-weight:600;}
QTextBrowser {background:white;border:1px solid #DFE7E9;border-radius:8px;padding:10px;}
QProgressBar {border:none;background:#DDE8E8;border-radius:4px;height:6px;}
QProgressBar::chunk {background:#258978;border-radius:4px;}
'''

def label(text,name=None):
    x=QLabel(text)
    if name:x.setObjectName(name)
    x.setWordWrap(True);return x


def button(text,fn,primary=False):
    x=QPushButton(text);x.setAutoDefault(False);x.clicked.connect(fn)
    action=auxiliary_action(text)
    if action:
        x.setObjectName('auxiliary');x.setProperty('auxiliaryAction',action)
        icon={'previous':QStyle.StandardPixmap.SP_ArrowBack,'next':QStyle.StandardPixmap.SP_ArrowForward,
              'retry':QStyle.StandardPixmap.SP_DialogResetButton,'refresh':QStyle.StandardPixmap.SP_BrowserReload,
              'close':QStyle.StandardPixmap.SP_TitleBarCloseButton,'stop':QStyle.StandardPixmap.SP_MediaStop}[action]
        x.setIcon(x.style().standardIcon(icon));x.setIconSize(QSize(18,18));x.setMinimumSize(36,34)
        x.setAccessibleName(text);x.setToolTip(text);x.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        if text in ('上一页','下一页','关闭'):x.setText('')
    elif primary:x.setObjectName('primary')
    return x


def auxiliary_action(text):
    """Only control verbs, never infer deletion or business confirmation actions."""
    for action,prefixes in [('previous',('上一页','上一步','返回','← 返回')),('next',('下一页','下一步')),
            ('retry',('重试','重跑','核对原任务并继续')),('refresh',('刷新','重新检查后台')),('close',('关闭',)),('stop',('停止',))]:
        if text.startswith(prefixes):return action
    return None


class FileCard(QFrame):
    changed=Signal(str)
    def __init__(self,title,description):
        super().__init__();self.path='';self.setObjectName('card');self.setAcceptDrops(True)
        v=QVBoxLayout(self);v.setContentsMargins(20,20,20,20);v.setSpacing(12)
        v.addWidget(label(title,'section'));v.addWidget(label(description,'muted'))
        self.filename=label('拖入 .xlsx 文件，或点击下方选择','muted');self.filename.setMinimumHeight(46);v.addWidget(self.filename)
        self.pick=button('选择 Excel 文件',self.choose);v.addWidget(self.pick,0,Qt.AlignmentFlag.AlignLeft)
        from import_ui import download
        v.addWidget(button('下载商品资料模板' if '资料' in title else '下载补货模板',lambda:download(self,'legacy' if '资料' in title else 'sales')))
    def choose(self):
        p,_=QFileDialog.getOpenFileName(self,'选择Excel文件',str(Path(self.path).parent) if self.path else '', 'Excel 工作簿 (*.xlsx)')
        if p:self.set_path(p)
    def set_path(self,p):
        self.path=str(Path(p));self.filename.setText(Path(p).name);self.filename.setToolTip(self.path);self.pick.setText('更换文件');self.changed.emit(self.path)
    def dragEnterEvent(self,event):
        if event.mimeData().hasUrls() and len(event.mimeData().urls())==1 and event.mimeData().urls()[0].toLocalFile().lower().endswith('.xlsx'):event.acceptProposedAction()
    def dropEvent(self,event):self.set_path(event.mimeData().urls()[0].toLocalFile());event.acceptProposedAction()


class SalesFiles(QFrame):
    changed=Signal()
    def __init__(self):
        super().__init__();self.paths=[];self.setAcceptDrops(True);self.setObjectName('card')
        v=QVBoxLayout(self);v.addWidget(label('补货建议 · 支持多文件、多门店','section'))
        v.addWidget(label('将一个或多个 .xlsx 拖到这里；自动按门店编码归集，不叠加重复库存。移出列表不会删除磁盘原文件。','muted'))
        self.list=QListWidget();self.list.setMinimumHeight(120);self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection);v.addWidget(self.list)
        row=QHBoxLayout();row.addWidget(button('添加补货文件',self.choose));
        from import_ui import download
        row.addWidget(button('下载补货模板',lambda:download(self,'sales')));row.addWidget(button('移出本次文件列表',self.remove));row.addStretch();v.addLayout(row)
    def choose(self):
        files,_=QFileDialog.getOpenFileNames(self,'添加补货建议','','Excel (*.xlsx)');self.add_paths(files)
    def add_paths(self,files):
        for p in files:
            path=str(Path(p).resolve())
            if path.lower().endswith('.xlsx') and path not in self.paths:self.paths.append(path)
        self.refresh();self.changed.emit()
    def refresh(self):
        self.list.clear()
        for p in self.paths:self.list.addItem(Path(p).name);self.list.item(self.list.count()-1).setToolTip(p)
    def remove(self):
        for i in sorted([self.list.row(x) for x in self.list.selectedItems()],reverse=True):self.paths.pop(i)
        self.refresh();self.changed.emit()
    def dragEnterEvent(self,event):
        if event.mimeData().hasUrls() and all(u.toLocalFile().lower().endswith('.xlsx') for u in event.mimeData().urls()):event.acceptProposedAction()
    def dropEvent(self,event):self.add_paths([u.toLocalFile() for u in event.mimeData().urls()]);event.acceptProposedAction()


