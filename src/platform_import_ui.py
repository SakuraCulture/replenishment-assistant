"""Shared controls for taking over an already prepared platform file."""
from pathlib import Path

from PySide6.QtWidgets import QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLineEdit, QVBoxLayout
from ui_controls import button, label


def operator_input(parent, title, description, *, order_label=None):
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.resize(600, 230)
    layout = QVBoxLayout(dialog)
    note = label(description, 'muted')
    note.setWordWrap(True)
    layout.addWidget(note)
    form = QFormLayout()
    order = QLineEdit()
    if order_label:
        form.addRow(order_label, order)
    operator, reason = QLineEdit(), QLineEdit()
    operator.setMaxLength(80)
    reason.setMaxLength(500)
    form.addRow('经办人', operator)
    form.addRow('核对依据', reason)
    layout.addLayout(form)
    actions = QHBoxLayout()
    actions.addWidget(button('继续', dialog.accept, True))
    actions.addWidget(button('取消', dialog.reject))
    layout.addLayout(actions)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return order.text().strip(), operator.text().strip(), reason.text().strip()


def export_target(parent, number, platform, suffix, usage):
    names = {'check':'原导入单·核对用', 'manual':'已接管·手工导入', 'failure':'平台失败明细'}
    safe = ''.join(c for c in number if c.isalnum() or c in '-_.')
    path, _ = QFileDialog.getSaveFileName(parent, names[usage], f'{safe}_{platform}_{names[usage]}{suffix}',
                                           f'Excel (*{suffix})')
    return path + suffix if path and not Path(path).suffix else path


def rows_dialog(parent, title, description, headers, rows):
    from business_ui import grid

    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.resize(1110, 600)
    layout = QVBoxLayout(dialog)
    note = label(description, 'muted')
    note.setWordWrap(True)
    layout.addWidget(note)
    search = QLineEdit()
    search.setPlaceholderText('按商品编码或关键词筛选')
    layout.addWidget(search)
    table, model = grid(dialog, headers, rows)
    search.textChanged.connect(model.filter)
    layout.addWidget(table, 1)
    layout.addWidget(button('关闭', dialog.accept))
    dialog.exec()
