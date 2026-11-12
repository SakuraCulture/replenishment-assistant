"""Focused navigation and presentation, sharing all existing business actions."""
from PySide6.QtCore import Qt, QSignalBlocker, QTimer, QObject, QEvent
from PySide6.QtWidgets import (QWidget, QFrame, QVBoxLayout, QHBoxLayout, QProgressBar,
    QToolButton, QTableView, QAbstractItemView, QComboBox)
from ui_controls import label, button
from history_tasks import HistoryReader
import business_flow


def disclosure(title, parent=None):
    toggle = QToolButton(parent)
    toggle.setText(title)
    toggle.setCheckable(True)
    toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    toggle.setArrowType(Qt.ArrowType.RightArrow)
    body = QWidget(parent)
    layout = QVBoxLayout(body)
    layout.setContentsMargins(0, 0, 0, 0)
    body.hide()
    def changed(checked):
        body.setVisible(checked)
        toggle.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)
    toggle.toggled.connect(changed)
    return toggle, body, layout


class HomeTasks(QFrame):
    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.data = None
        self.busy = False
        self.reader = HistoryReader(host.work_repository.path, host.repository.folder / 'lifecycle_index.sqlite3', self)
        self.setObjectName('card')
        v = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(label('下一步处理 · 全部未完成订单，不限日期', 'section'), 1)
        self.refresh_btn = button('刷新待办', self.refresh)
        row.addWidget(self.refresh_btn)
        self.stop_btn = button('停止查询', self.stop)
        row.addWidget(self.stop_btn)
        v.addLayout(row)
        row = QHBoxLayout()
        self.buttons = {}
        self.names = {'return': '待回传', 'operations':'待运营签发', 'payment': '待付款核对', 'receipt': '待收货核对', 'attention': '需重新核对'}
        for key, title in self.names.items():
            b = button(title + '  —', lambda checked=False, k=key: self.open_group(k))
            self.buttons[key] = b
            b.setEnabled(False)
            row.addWidget(b)
        v.addLayout(row)
        self.note = label('正在准备待办查询…', 'muted')
        v.addWidget(self.note)
        self.progress = QProgressBar()
        self.progress.setMaximumHeight(16)
        self.progress.hide()
        v.addWidget(self.progress)
        self.batch_btn = button('查看最近未完成分配批次', self.open_batch)
        self.batch_btn.setEnabled(False)
        v.addWidget(self.batch_btn)
        self.stop_btn.hide()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.timeout.connect(self.refresh)

    def refresh(self):
        if self.host.closing or self.busy:
            return
        self.busy = True
        self.data = None
        self.refresh_btn.setEnabled(False)
        self.stop_btn.show()
        self.batch_btn.setEnabled(False)
        for b in self.buttons.values():
            b.setEnabled(False)
        self.progress.setRange(0, 0)
        self.progress.show()
        self.note.setText('正在读取单据状态，可继续使用其他页面；首次索引可能较慢。')
        self.reader.request({'kind': 'business_home'}, self.ready, self.failed, self.progressed)

    def progressed(self, p):
        self.progress.setRange(0, max(1, p['total']))
        self.progress.setValue(p['done'])
        self.note.setText(f"正在读取签发 {p['done']} / {p['total']}；这不是业务完成进度。")

    def finish(self):
        self.busy = False
        self.progress.hide()
        self.stop_btn.hide()
        self.refresh_btn.setEnabled(True)

    def ready(self, data):
        self.finish()
        self.data = data
        for key, b in self.buttons.items():
            n = len(data['groups'][key])
            b.setText(self.names[key] + f'  {n} 单')
            b.setEnabled(bool(n))
        b = data['latest_batch']
        self.batch_btn.setEnabled(b is not None)
        suffix = ' · 暂无'
        if b:
            from datetime import datetime
            try: created = datetime.fromisoformat(b['created']).strftime('%Y-%m-%d %H:%M')
            except (ValueError, KeyError): created = '时间待核'
            pending = sum(b['counts'].get(s, 0) for s in ('pending', 'failed'))
            suffix = f' · {created} · 待处理 {pending} 单'
        self.batch_btn.setText('查看最近未完成分配批次' + suffix)
        self.note.setText(f"已查询 {data['total']} 个店仓订单。各环节按当前缺失资料统计；需重新核对可能与环节待办重叠，不相加。")

    def stop(self):
        self.reader.cancel()
        self.finish()
        self.note.setText('待办查询已停止，未改变任何订单；点击刷新可重试。')

    def failed(self, text):
        self.finish()
        self.note.setText('待办读取失败：' + text + '。点击刷新重试，工作区仍可使用。')

    def clean_overview(self):
        w = self.host.followup
        blockers = [QSignalBlocker(x) for x in (w.status, w.search, w.warehouse_filter, w.lock_filter)]
        w.start.clear(); w.end.clear(); w.search.clear()
        w.status.setCurrentIndex(0); w.warehouse_filter.setCurrentIndex(0); w.lock_filter.setCurrentIndex(0)
        w.checked.clear()
        del blockers
        return w

    def open_group(self, key):
        if not self.data:
            return
        heads = self.data['groups'][key]
        if not heads:
            return
        w = self.clean_overview()
        if key == 'attention':
            from lifecycle_home_ui import LifecycleWidget
            from PySide6.QtWidgets import QDialog
            d = QDialog(self.host); d.setWindowTitle('需重新核对 · 限定首页所列订单'); d.resize(1360, 850)
            layout = QVBoxLayout(d)
            panel = LifecycleWidget(self.host, scope=heads)
            panel.start.clear(); panel.end.clear()
            layout.addWidget(panel)
            layout.addWidget(button('关闭', d.reject))
            QTimer.singleShot(0, panel.query)
            try:
                d.exec()
            finally:
                panel.shutdown(); d.deleteLater()
        else:
            w.open_stage(key, heads=heads)
        self.refresh()

    def open_batch(self):
        if not self.data or not self.data['latest_batch']:
            return
        from lifecycle_home_ui import StageDialog
        d = StageDialog(self.clean_overview(), 'jst')
        d.panel.batch_id = self.data['latest_batch']['batch_id']
        d.panel.scope = None
        try:
            d.exec()
        finally:
            d.deleteLater()
        self.refresh()


class FixedColumns(QObject):
    """Overlay shares the original model/selection; no row copies or reordering."""
    def __init__(self, table, columns=(0, 1)):
        super().__init__(table)
        self.table = table
        self.columns = columns
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.sync)
        view = self.view = QTableView(table)
        view.setModel(table.model())
        view.setSelectionModel(table.selectionModel())
        view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        view.setSelectionBehavior(table.selectionBehavior())
        view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setVerticalScrollMode(table.verticalScrollMode())
        view.verticalHeader().hide()
        view.setAlternatingRowColors(True)
        for c in range(table.columnCount()):
            view.setColumnHidden(c, c not in columns)
        view.clicked.connect(lambda i: table.cellClicked.emit(i.row(), i.column()))
        table.verticalScrollBar().valueChanged.connect(view.verticalScrollBar().setValue)
        view.verticalScrollBar().valueChanged.connect(table.verticalScrollBar().setValue)
        table.horizontalHeader().sectionResized.connect(lambda *_: self.sync())
        table.verticalHeader().sectionResized.connect(lambda *_: self.sync())
        table.model().rowsInserted.connect(lambda *_: self.timer.start(0))
        table.model().modelReset.connect(lambda *_: self.timer.start(0))
        table.installEventFilter(self)
        self.sync()

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.LayoutRequest):
            self.sync()
        return False

    def sync(self):
        from shiboken6 import isValid
        if not isValid(self.table) or not isValid(self.view):
            return
        t, v = self.table, self.view
        for c in self.columns:
            v.setColumnWidth(c, t.columnWidth(c))
        for r in range(t.rowCount()):
            v.setRowHeight(r, t.rowHeight(r)); v.setRowHidden(r, t.isRowHidden(r))
        left = t.verticalHeader().width() if t.verticalHeader().isVisible() else 0
        width = sum(t.columnWidth(c) for c in self.columns) + 2 * t.frameWidth()
        v.setGeometry(left, 0, width, t.viewport().height() + t.horizontalHeader().height() + 2*t.frameWidth())
        v.horizontalHeader().setFixedHeight(t.horizontalHeader().height())
        v.show(); v.raise_()


def install_review(host):
    row = QHBoxLayout()
    row.addWidget(label('核对范围'))
    host.review_filter = QComboBox()
    host.review_filter.addItems(['全部门店', '阻断：失败 / 待重算', '待核：数据 / 缺价 / 最低持有', '已完成可核对'])
    row.addWidget(host.review_filter)
    host.review_hint = label('', 'muted')
    row.addWidget(host.review_hint, 1)
    host.overview_page.layout().insertLayout(4, row)
    host.fixed_overview = FixedColumns(host.overview)
    host.review_filter.currentIndexChanged.connect(lambda *_: refresh_review(host))


def refresh_review(host):
    if not hasattr(host, 'review_filter'):
        return
    selected = (None, 'blocking', 'review', 'ready')[host.review_filter.currentIndex()]
    counts = dict.fromkeys(('blocking', 'review', 'pending', 'ready'), 0)
    visible = 0
    for i, store in enumerate(host.overview_codes):
        kind = business_flow.review_state(host.states[store]['state'], host.results.get(store))
        counts[kind] += 1
        show = selected is None or kind == selected or selected == 'ready' and kind == 'review'
        host.overview.setRowHidden(i, not show)
        visible += show
    host.review_hint.setText(f"显示 {visible} / {len(host.overview_codes)} 店；阻断 {counts['blocking']} 店，待核 {counts['review']} 店。正常预算未满足不算阻断；导出范围仍由原导出窗口选择。")
    host.fixed_overview.sync()
