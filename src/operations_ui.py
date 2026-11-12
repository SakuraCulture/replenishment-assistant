"""Operations reuses the return review. These actions never call a platform."""
from PySide6.QtWidgets import QMessageBox
from business_ui import run
from lifecycle_ui import Review,commit,warn
import operations_flow


def adopt(parent,host,heads):
    if not heads:
        QMessageBox.information(parent,'请先勾选','勾选要采用回传的仓单；每单独立保留原回传。');return
    try:
        plan=run(parent,operations_flow.adopt_plan,(host.work_repository.path,heads),'采用已确认回传')
        # A deliberate replacement gets the same difference review as returns.
        if any(p['expected'] and p['changed'] for p in plan['plans']):
            d=Review(parent,plan)
            try:
                if not d.exec():return
            finally:d.deleteLater()
        result=commit(parent,host,plan)
        parent.imported()
        return result
    except Exception as exc:warn(parent,exc)


def forward(parent,host,heads):
    if not heads:
        QMessageBox.information(parent,'请先勾选','勾选运营已确认的仓单；分配后直接准备付款，无需搬表。');return
    from lifecycle_home_ui import StageDialog
    from jst_results import key
    d=StageDialog(parent,'jst',heads)
    previous=getattr(host,'_large_io_dialog',None);host._large_io_dialog=d
    d.panel.auto_payment_scope={key(h) for h in heads}
    d.panel.status.setText('先核对或完成本范围分配；结果就绪后自动打开付款核对，未确认前不会保存付款。')
    try:d.exec()
    finally:host._large_io_dialog=previous;d.deleteLater();parent.query(keep_page=True)
