"""Pick signed orders or a business date, then confirm the full export scope."""
from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QDateEdit, QDialog, QFileDialog, QHBoxLayout, QMessageBox, QTableWidget, QTableWidgetItem, QVBoxLayout

from business_ui import run
from runtime_support import Cancelled
from ui_controls import button, label


def _table(headers, rows):
    table = QTableWidget(len(rows), len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    for index, row in enumerate(rows):
        for column, value in enumerate(row):
            table.setItem(index, column, QTableWidgetItem(str(value)))
    table.resizeColumnsToContents()
    table.horizontalHeader().setStretchLastSection(True)
    return table


def open_selection(parent, host, checked):
    from normal_orders import Repository
    from order_migration_ui import idle
    from order_selection import preview, export

    headers = Repository(host.work_repository).headers()
    if not headers:
        QMessageBox.information(parent, '导出所选订单迁移包', '暂无可迁移的签发订单。')
        return
    root = host.repository.folder
    dialog = QDialog(parent)
    dialog.setWindowTitle('选择要迁移的订单')
    dialog.resize(830, 530)
    layout = QVBoxLayout(dialog)
    layout.addWidget(label('选择业务日或勾选订单。迁移包用于合并导入。'))
    date_row = QHBoxLayout()
    date_row.addWidget(label('业务日'))
    business_date = QDateEdit(QDate.currentDate(), calendarPopup=True, displayFormat='yyyy-MM-dd')
    business_date.setObjectName('orderSelectionDate')
    date_row.addWidget(business_date)
    layout.addLayout(date_row)
    table = _table(('勾选', '业务日', '门店编码', '补货单号', '签发修订'),
                   [('', row['day'], row['store'], row['number'], row['revision']) for row in headers])
    table.setObjectName('orderSelectionTable')
    for index, row in enumerate(headers):
        item = table.item(index, 0)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if row['oid'] in checked else Qt.CheckState.Unchecked)
    layout.addWidget(table)

    def inspect_scope(by_date):
        try:
            idle(host)
            day = business_date.date().toString('yyyy-MM-dd') if by_date else ''
            ids = () if by_date else tuple(row['oid'] for index, row in enumerate(headers)
                                           if table.item(index, 0).checkState() == Qt.CheckState.Checked)
            if not day and not ids:
                raise ValueError('请先勾选要迁移的签发订单')
            scope = run(dialog, preview, (root, ids, day), '核对迁移订单及完整关联')
            review = QDialog(dialog)
            review.setWindowTitle('核对所选订单迁移范围')
            review.resize(980, 480)
            body = QVBoxLayout(review)
            chosen = len(scope['requested'])
            body.addWidget(label(f'原选 {chosen} 单 · 关联带入 {len(scope["orders"]) - chosen} 单 · 仅合并导入'))
            details = _table(('业务日', '门店', '补货单号', '仓', '签发修订', '入包依据'),
                             [(r['day'], r['name'], r['number'], '、'.join(r['warehouses']), r['revision'],
                               '原选' if r['requested'] else '共享分配、采购或作业关联') for r in scope['orders']])
            details.setObjectName('orderSelectionPreview')
            body.addWidget(details)
            actions = QHBoxLayout()
            accept = button('确认导出迁移包', review.accept, True)
            accept.setObjectName('confirmOrderSelection')
            actions.addWidget(accept)
            actions.addWidget(button('返回调整范围', review.reject))
            body.addLayout(actions)
            if review.exec() != QDialog.DialogCode.Accepted:
                return
            path, _ = QFileDialog.getSaveFileName(dialog, '导出所选订单迁移包', '补货助手_所选订单迁移.zip', 'Zip (*.zip)')
            if not path:
                return
            idle(host)
            result = run(dialog, export, (root, path, scope), '冻结并导出所选订单及关联资料')
            missing = len(result['manifest']['missing_external'])
            text = f"{result['path']}\n本包仅用于合并导入。\n原路径缺失记录 {missing} 项，留存的原件随包保存。\n校验摘要 {result['sha256']}"
            QMessageBox.information(dialog, '所选订单迁移包已导出', text)
            dialog.accept()
        except Cancelled:
            return
        except Exception as exc:
            QMessageBox.warning(dialog, '所选订单迁移包未导出', str(exc))

    by_date = button('核对该业务日订单', lambda: inspect_scope(True))
    by_date.setObjectName('previewByBusinessDate')
    date_row.addWidget(by_date)
    by_selection = button('核对勾选订单', lambda: inspect_scope(False), True)
    by_selection.setObjectName('previewSelectedOrders')
    layout.addWidget(by_selection)
    layout.addWidget(button('关闭', dialog.reject))
    dialog.exec()
