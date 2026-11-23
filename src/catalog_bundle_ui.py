"""Read-only component diagnostics for the running or an older delivery."""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from business_ui import run
from catalog_sync_bundle import ComponentIntegrityError, describe_component, verify_component
from runtime_support import Cancelled, checkpoint


def show_result(parent, title, summary, details, *, ok=False):
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setIcon(QMessageBox.Icon.Information if ok else QMessageBox.Icon.Warning)
    box.setText(summary)
    box.setDetailedText(details)
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    copy = box.addButton('复制核对结果', QMessageBox.ButtonRole.ActionRole)
    copy.clicked.connect(lambda: QApplication.clipboard().setText(summary+'\n\n'+details))
    box.exec()


def inspect_folder(folder, progress=lambda _: None, cancel=None):
    base = Path(folder)/'_internal'/'components'
    jst, ax = base/'jst_catalog', base/'aoxiang'
    checkpoint(cancel)
    progress('检查文件是否齐全并校验内容…')
    try:
        manifest = verify_component(jst, ax)
    except ComponentIntegrityError as exc:
        return False, str(exc), exc.details
    checkpoint(cancel)
    summary = f'组件校验通过。已核对聚水潭文件 {len(manifest["files"])} 个，共用文件 {len(manifest["shared"])} 个。'
    return True, summary, describe_component(jst, ax, manifest)


def inspect_installation(parent):
    folder = QFileDialog.getExistingDirectory(parent, '选择要核对的程序目录（补货助手.exe 所在文件夹）')
    if not folder:
        return
    try:
        ok, summary, details = run(parent, inspect_folder, (folder,), '核对聚水潭组件文件')
    except Cancelled:
        return
    except Exception as exc:
        QMessageBox.warning(parent, '组件核对未完成', str(exc))
        return
    show_result(parent, '聚水潭组件文件核对', summary, details, ok=ok)
