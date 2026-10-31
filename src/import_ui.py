"""One drop/picker/template entry used by all import flows."""
import export_locations
from pathlib import Path
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QFileDialog,QListWidget,QMessageBox
from ui_controls import button,label
import import_templates

def download(parent,kind):
    path,_=export_locations.save_file(parent,'下载最低标准模板',import_templates.TEMPLATES[kind][0]+'模板.xlsx','Excel (*.xlsx)')
    if not path:return
    if not path.lower().endswith('.xlsx'):path+='.xlsx'
    try:import_templates.write(kind,path);QMessageBox.information(parent,'模板已保存',path)
    except Exception as exc:QMessageBox.warning(parent,'模板未保存',str(exc))

class ImportDialog(QDialog):
    def __init__(self,parent,kind,title=None,multiple=False):
        super().__init__(parent);self.kind=kind;self.multiple=multiple;self.paths=[];self.extensions=('.xlsx','.csv','.json') if kind=='priority' else ('.xlsx',);self.setWindowTitle(title or '导入'+import_templates.TEMPLATES[kind][0]);self.setAcceptDrops(True);self.resize(650,330)
        v=QVBoxLayout(self);v.addWidget(label('拖入文件，或点击选择。读取校验后再进入后续核对；原文件不修改。','section'));self.list=QListWidget();v.addWidget(self.list);row=QHBoxLayout();row.addWidget(button('选择文件',self.choose));row.addWidget(button('下载'+import_templates.TEMPLATES[kind][0]+'模板',lambda:download(self,kind)));v.addLayout(row);row=QHBoxLayout();self.go=button('读取并校验',self.accept,True);self.go.setEnabled(False);row.addWidget(self.go);row.addWidget(button('取消',self.reject));v.addLayout(row)
    def set_paths(self,paths):
        if not paths or (not self.multiple and len(paths)!=1) or any(Path(p).suffix.lower() not in self.extensions for p in paths):return
        self.paths=list(dict.fromkeys(str(Path(p).resolve()) for p in paths));self.list.clear();self.list.addItems(self.paths);self.go.setEnabled(True)
    def choose(self):
        filt='支持的文件 ('+' '.join('*'+x for x in self.extensions)+')'
        if self.multiple:paths,_=QFileDialog.getOpenFileNames(self,'选择文件','',filt)
        else:
            path,_=QFileDialog.getOpenFileName(self,'选择文件','',filt);paths=[path] if path else []
        self.set_paths(paths)
    def dragEnterEvent(self,event):
        urls=event.mimeData().urls()
        if urls and (self.multiple or len(urls)==1) and all(u.isLocalFile() and Path(u.toLocalFile()).suffix.lower() in self.extensions for u in urls):event.acceptProposedAction()
    def dropEvent(self,event):self.set_paths([u.toLocalFile() for u in event.mimeData().urls()]);event.acceptProposedAction()

def choose(parent,kind,title=None,multiple=False):
    d=ImportDialog(parent,kind,title,multiple);return d.paths if d.exec()==QDialog.DialogCode.Accepted else []
