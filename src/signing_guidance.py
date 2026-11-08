"""Explain existing pending-edit protection before opening sign/write workflows."""
from PySide6.QtWidgets import QMessageBox


def pending_stores(host,codes):
    pending=getattr(host,'preissue_drafts',{})
    return [s for s in codes if s in pending]


def require_applied(host,codes):
    pending=pending_stores(host,codes)
    if not pending:return True
    names=['• '+host.states.get(s,{}).get('name',s)+'（'+s+'）' for s in pending]
    box=QMessageBox(host);box.setWindowTitle('请先应用最终清单调整');box.setIcon(QMessageBox.Icon.Warning)
    box.setText('以下门店的最终清单调整尚未应用，暂不能确认签发或导出：\n'+'\n'.join(names))
    box.setInformativeText('打开最终清单，补齐调整原因，核对修改与预算，确认风险后点击“应用最终数量”。\n“保留为待核对草稿”只暂存。应用后请再次点击确认签发。')
    cancel=box.addButton('返回继续核对',QMessageBox.ButtonRole.RejectRole)
    review=box.addButton('打开这些门店的最终清单',QMessageBox.ButtonRole.ActionRole)
    box.setDefaultButton(cancel);box.setEscapeButton(cancel);box.exec()
    if box.clickedButton() is review:host.open_final_list(codes=pending)
    return False
