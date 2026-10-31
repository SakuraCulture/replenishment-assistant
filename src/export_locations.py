"""One default location for export/save dialogs. Temporary choices never persist."""
import os
from pathlib import Path
from PySide6.QtCore import QStandardPaths, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLineEdit, QFileDialog, QMessageBox, QApplication
from ui_controls import button, label
import export_settings as settings

def config_path(parent):
    obj = parent
    while obj is not None:
        repo = getattr(obj, 'repository', None)
        if repo is not None and hasattr(repo, 'folder'): return Path(repo.folder)/settings.FILENAME
        obj = obj.parentWidget() if hasattr(obj, 'parentWidget') else None
    folder = os.environ.get('REPLENISHMENT_DATA_DIR') or str(Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation))/'settings')
    return Path(folder)/settings.FILENAME

def initial_directory(parent, fallback=''):
    try:
        configured = settings.load(config_path(parent))
        if configured: return settings.validate(configured)
    except (OSError, ValueError) as exc:
        QMessageBox.warning(parent, '默认导出位置暂不可用', str(exc)+'\n请在本次窗口重新选择保存位置，或打开“导出设置”修改。原配置保留。')
    if fallback and Path(fallback).is_dir(): return str(fallback)
    return QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)

def save_file(parent, caption, filename='', file_filter='', **kwargs):
    p = Path(filename) if filename else None
    fallback = str(p.parent) if p and p.is_absolute() else ''
    folder = initial_directory(parent, fallback)
    proposed = str(Path(folder)/p.name) if p else folder
    return QFileDialog.getSaveFileName(parent, caption, proposed, file_filter, **kwargs)

def directory(parent, caption, initial='', **kwargs):
    return QFileDialog.getExistingDirectory(parent, caption, initial_directory(parent, initial), **kwargs)

class ExportSettingsDialog(QDialog):
    def __init__(self, host):
        super().__init__(host); self.path=config_path(host); self.setWindowTitle('导出设置'); self.resize(790,340)
        v=QVBoxLayout(self); v.addWidget(label('默认导出位置','section')); row=QHBoxLayout(); self.directory=QLineEdit(); self.directory.setPlaceholderText('未设置时使用系统文档目录'); row.addWidget(self.directory,1); row.addWidget(button('选择文件夹',self.browse)); v.addLayout(row)
        row=QHBoxLayout(); row.addWidget(button('使用系统文档目录',lambda:self.directory.setText(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)))); row.addStretch(); v.addLayout(row)
        v.addWidget(label('适用于确认单、内部明细、归档、生命周期、原件和模板。每次仍可临时改选；临时选择不会修改默认值。','muted'))
        v.addWidget(label('配置文件存储位置（随本机资料保留，升级程序后继续使用）'))
        self.location=QLineEdit(str(self.path)); self.location.setReadOnly(True); v.addWidget(self.location)
        row=QHBoxLayout(); row.addWidget(button('复制配置文件位置',lambda:QApplication.clipboard().setText(str(self.path)))); row.addWidget(button('打开配置文件夹',self.open_config_folder)); row.addStretch(); v.addLayout(row)
        self.note=label('只保存导出偏好，不移动已有文件或修改计算参数。','muted'); v.addWidget(self.note)
        try:self.directory.setText(settings.load(self.path))
        except (OSError,ValueError) as exc:self.note.setText(str(exc)+'；修正后点击保存，取消则保留原配置。')
        row=QHBoxLayout(); cancel=button('取消',self.reject); cancel.setDefault(True); row.addWidget(cancel); self.save_btn=button('保存默认导出位置',self.save,True); self.save_btn.setAutoDefault(False); row.addWidget(self.save_btn); v.addLayout(row)
    def browse(self):
        initial=self.directory.text().strip()
        if not initial or not Path(initial).is_dir():initial=QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        selected=QFileDialog.getExistingDirectory(self,'选择默认导出文件夹',initial)
        if selected:self.directory.setText(selected)
    def open_config_folder(self):
        if self.path.parent.is_dir():QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path.parent)))
        else:QMessageBox.information(self,'配置文件夹尚未创建','保存设置后会创建：'+str(self.path.parent))
    def save(self):
        try:settings.save(self.path,self.directory.text());self.accept()
        except (OSError,ValueError) as exc:QMessageBox.warning(self,'设置未保存',str(exc))
