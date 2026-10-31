"""Four outcome-oriented entrypoints; existing business services remain shared."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox, QPushButton
from ui_controls import button, label

SCENES = [('补货出单', 1), ('订单跟踪', 3), ('决策复查', 2), ('参数设置', 4)]


def clear_layout(layout, keep=()):
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            widget = item.widget()
            if widget not in keep:
                widget.hide()
                widget.deleteLater()
        elif item.layout():
            clear_layout(item.layout(), keep)
            item.layout().deleteLater()


def install(host, home_layout):
    """Keep established section IDs so historical and smoke callers stay valid."""
    home_layout.removeWidget(host.followup)
    orders = QWidget()
    v = QVBoxLayout(orders)
    v.addWidget(label('订单跟踪', 'title'))
    v.addWidget(label('按补货单号保存回传、付款和实际收货；点每单的下一步即可继续。', 'muted'))
    v.addWidget(host.followup, 1)
    host.sections.addWidget(orders)  # 3
    from parameter_scene import ParameterScene
    host.parameter_scene = ParameterScene(host)
    host.sections.addWidget(host.parameter_scene)  # 4
    host.top_nav[1].setText('补货出单')
    host.top_nav[2].setText('决策复查')
    nav = host.centralWidget().layout().itemAt(0).layout()
    tracking = button('订单跟踪', lambda: host.show_section(3))
    settings = button('参数设置', lambda: host.show_section(4))
    nav.insertWidget(2, tracking)
    nav.insertWidget(4, settings)
    host.top_nav.extend([tracking, settings])

    clear_layout(home_layout, (host.home_status, host.recent))
    home_layout.addWidget(label('今天要完成什么？', 'title'))
    home_layout.addWidget(host.home_status)
    cards = QGridLayout()
    descriptions = [
        ('准备门店数据，核对建议，取得分仓签发单。', '进入补货出单'),
        ('收到回传、付款或收货资料，保存到对应订单。', '进入订单跟踪'),
        ('对某次建议有异议，查当时事实、参数和数量变化。', '进入决策复查'),
        ('维护门店主从仓、默认参数和可复用参数方案。', '进入参数设置'),
    ]
    for i, ((title, index), (description, action)) in enumerate(zip(SCENES, descriptions)):
        group = QGroupBox(title)
        layout = QVBoxLayout(group)
        layout.addWidget(label(description))
        layout.addStretch()
        layout.addWidget(button(action, lambda checked=False, n=index: host.show_section(n), True))
        cards.addWidget(group, i // 2, i % 2)
    home_layout.addLayout(cards, 1)
    row = QHBoxLayout()
    row.addWidget(button('继续已保存的今日草稿', host.resume_today))
    row.addWidget(button('全部未收齐订单', lambda: open_tracking(host, pending=True)))
    row.addStretch()
    row.addWidget(button('数据维护', host.storage_copy))
    home_layout.addLayout(row)
    home_layout.addWidget(label('签发、门店回传、门店付款、门店实际收货分别保存；原签发和当时依据可随时复查。使用指南：F1。', 'muted'))
    host.nav[3].setText('④ 核对与签发')
    host.issue_btn.setText('确认签发')
    host.merchant_btn.setText('确认签发并导出')
    host.overview_page.layout().addWidget(label('签发后自动归档；导出失败可按原签发重试。', 'muted'))


def open_tracking(host, number='', pending=False):
    from PySide6.QtCore import QSignalBlocker
    widget = host.followup
    if number or pending:
        widget.start.clear()
        widget.end.clear()
        widget.search.setText(number)
        blocker = QSignalBlocker(widget.status)
        widget.status.setCurrentIndex(1 if pending else 0)
        del blocker
    host.show_section(3)
    if number or pending:widget.query()


def issue_result(host, receipt):
    """Persistent result, no hidden sign-again action on export retry."""
    from PySide6.QtWidgets import QDialog
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtCore import QUrl
    from pathlib import Path
    paths = receipt.get('paths', [])
    dialog = QDialog(host)
    dialog.setWindowTitle('签发与导出结果')
    dialog.resize(760, 390)
    layout = QVBoxLayout(dialog)
    layout.addWidget(label(f'已生成 {len(paths)} 份签发单文件', 'section'))
    layout.addWidget(label('已签发内容独立保留；下次导出已有签发请进入订单跟踪。'))
    from business_ui import grid
    table, _ = grid(dialog, ['文件'], [[str(p)] for p in paths])
    table.setColumnWidth(0, 690)
    layout.addWidget(table, 1)
    row = QHBoxLayout()
    if paths:
        row.addWidget(button('打开导出目录', lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(paths[0]).parent)))))
    def current_orders():
        dialog.accept();host.followup.start.setText(host.job_day);host.followup.end.setText(host.job_day);host.followup.search.clear();host.followup.checked.clear();host.tracking_stale=True
        from PySide6.QtCore import QSignalBlocker
        blocker=QSignalBlocker(host.followup.status);host.followup.status.setCurrentIndex(0);del blocker
        open_tracking(host)
    row.addWidget(button('查看当日订单跟踪', current_orders))
    row.addWidget(button('继续本次补货', dialog.accept))
    layout.addLayout(row)
    dialog.exec()
