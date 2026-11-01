"""One adoption boundary into the existing check/import path; existing state stays until success."""
from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import QMessageBox
import batch_service
from aoxiang_acquisition import verify_loaded


def adopt(host, value):
    if host.worker or host.readonly or host.closing:
        return False
    products = host.products.path if host.master_legacy.isChecked() else host.catalogs
    if not products:
        QMessageBox.information(host, '先准备商品资料', '采集文件已保留。请先导入对应仓商品资料，再从翱象采集记录采用本次文件。')
        return False
    if host.loaded or host.results or host.sales.paths:
        answer = QMessageBox.question(host, '采用本次采集文件',
            f'将用本次 {len(value["files"])} 份已验收文件替换当前补货建议批次。检查通过后原结果和人工调整需要重新计算。\n原草稿和签发历史保留。是否继续？',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return False
    if not host.leave_current('采用翱象采集文件'):
        return False
    host.auto_check.stop()
    paths = [item['file'] for item in value['files']]

    def success(loaded):
        try:
            verify_loaded(value, loaded)
        except Exception as exc:
            host.failed(str(exc))
            return
        with QSignalBlocker(host.sales):
            host.sales.paths = paths
            host.sales.refresh()
        host.loaded_ok(loaded)
        host.status.setText(f'翱象 {len(paths)} 份文件已检查并采用；沿用已保存门店参数。请核对冲突、仓设置及预算后计算。')
    # Keep exact function identity: Worker retains originals and applies its existing cancellation guard.
    host.job(batch_service.load, (paths, products), success)
    return True
