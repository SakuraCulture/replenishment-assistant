from PySide6.QtWidgets import QMessageBox

from business_ui import run
from interaction_ui import review_rows
from lifecycle_ui import warn
from platform_import_ui import operator_input
import platform_remainders as service


def show(parent, host, report):
    rows=[[r['sku'],r['request'],r['unit'],r['actual'],r['actual_unit'],r['remainder'],r['status']] for r in report['rows']]
    title=report['head']['number']+' · '+report['platform']+' '+report['platform_order']
    if not review_rows(parent,'核对平台差额',['商品编码','原提交量','原基础单位','平台数量','平台基础单位','数量差额','核对结果'],
                       rows,title+'\n确认后保存本次差额核对依据。','登记核对结果'):
        return
    values=operator_input(parent,'保存差额核对','填写经办人及本次核对依据。')
    if not values:return
    _,operator,reason=values
    try:
        result=run(parent,service.save,(host.work_repository.path,report,operator,reason),'保存差额核对')
        QMessageBox.information(parent,'已保存差额核对','核对记录 '+result['id'][:12]+' · '+result['at'])
    except Exception as exc:
        warn(parent,exc)
