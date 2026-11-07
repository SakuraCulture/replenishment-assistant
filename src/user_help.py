"""Open bundled help on demand; never load documentation during startup."""
import sys
from pathlib import Path


def location():
    root=Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[1]
    return root/('使用手册' if getattr(sys,'frozen',False) else 'docs/manual')/'index.html'


def show(parent):
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import QMessageBox
    path=location()
    if not path.is_file():
        QMessageBox.information(parent,'使用手册未找到','请将完整程序包解压后运行；手册应位于：\n'+str(path));return
    if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
        QMessageBox.information(parent,'手册已定位','请在文件管理器打开：\n'+str(path))
