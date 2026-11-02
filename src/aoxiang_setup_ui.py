"""Business-facing AX configuration; all filesystem/browser work stays off the UI thread."""
from pathlib import Path
import sys
import time

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QComboBox,
                             QLineEdit, QTableWidget, QTableWidgetItem, QHeaderView,
                             QAbstractItemView, QCheckBox, QMessageBox, QProgressBar)
from ui_controls import button, label
from aoxiang_client import ProcessClient
from aoxiang_directory import Directory, read_directory


from aoxiang_ui_common import ComponentDialog, Call, component_root, show_modal


class SetupDialog(ComponentDialog):
    def __init__(self, parent, folder, *, client_factory=ProcessClient):
        super().__init__(parent)
        self.setWindowTitle('翱象连接与门店配置')
        self.resize(980, 720)
        self.folder = Path(folder) / 'aoxiang'
        self.directory = Directory(self.folder)
        self.init_client(parent, client_factory)
        self.started = False
        self.worker = None
        self.setup_id = None
        self.setup_state = None
        self.setup_cancel_requested = False
        self._closing = False
        self._can_close = False
        self._close_done = False
        self.cancel_pending = False
        self.transport_failed = False
        self.reconnecting = False
        self.detect_pending = False
        self.last_status = None
        self.status_received = time.monotonic()
        self.data = None
        self.checks = []
        self.poll = QTimer(self)
        self.poll.setInterval(600)
        self.poll.timeout.connect(self.refresh_setup)
        self.clock = QTimer(self)
        self.clock.setInterval(1000)
        self.clock.timeout.connect(self.render_status)
        layout = QVBoxLayout(self)
        layout.addWidget(label('先维护门店目录，再核验登录和所选门店，最后保存。此处只查询，不创建导出或采购单。', 'section'))
        self.note = label('门店按编码关联，名称用于辅助定位。未核验的目录不作为已验证平台绑定。', 'muted')
        layout.addWidget(self.note)
        row = QHBoxLayout()
        self.import_btn = button('导入门店目录', self.import_directory)
        row.addWidget(self.import_btn)
        self.template_btn = button('下载门店目录模板', self.download_template)
        row.addWidget(self.template_btn)
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索门店名称或编码')
        self.search.textChanged.connect(self.filter_rows)
        row.addWidget(self.search, 1)
        self.all_btn = button('勾选当前筛选', lambda: self.select_visible(True))
        self.none_btn = button('清空全部勾选', self.clear_selection)
        row.addWidget(self.all_btn)
        row.addWidget(self.none_btn)
        self.selection_count = QLabel('已选 0 家')
        row.addWidget(self.selection_count)
        layout.addLayout(row)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['选择', '门店名称', '门店编码', '平台核验'])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 55)
        self.table.setColumnWidth(2, 190)
        self.table.setColumnWidth(3, 300)
        layout.addWidget(self.table, 1)
        form = QFormLayout()
        self.edge = QComboBox()
        self.profile = QComboBox()
        self.detect_btn = button('检测本机已有浏览器', self.detect)
        erow = QHBoxLayout()
        erow.addWidget(self.edge, 1)
        erow.addWidget(self.detect_btn)
        form.addRow('Edge 浏览器', erow)
        form.addRow('已有用户配置', self.profile)
        self.output = QLineEdit(str((self.folder / 'downloads').resolve()))
        form.addRow('采集文件保存位置', self.output)
        layout.addLayout(form)
        suppliers = QHBoxLayout()
        self.supplier_edits = {}
        for warehouse in ['山东仓', '河北仓']:
            suppliers.addWidget(QLabel(warehouse + '供应商编码'))
            edit = QLineEdit()
            self.supplier_edits[warehouse] = edit
            suppliers.addWidget(edit)
        self.supplier_btn = button('保存供应商对应', self.save_suppliers)
        suppliers.addWidget(self.supplier_btn)
        layout.addLayout(suppliers)
        layout.addWidget(label('供应商对应用于后续采购上传，不改变补货计算的主从仓和库存单位。', 'muted'))
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        self.status = label('尚未连接。请导入目录，勾选本次门店，再检测浏览器。', 'muted')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.verify_btn = button('核验登录与所选门店', self.verify, True)
        self.stop_btn = button('停止本次核验', self.stop_setup)
        self.reconnect_btn = button('异常后重新连接', self.reconnect)
        self.save_btn = button('保存已核验连接与门店', self.save_setup, True)
        self.close_btn = button('关闭', self.close)
        for widget in [self.verify_btn, self.stop_btn, self.reconnect_btn, self.save_btn, self.close_btn]:
            actions.addWidget(widget)
        layout.addLayout(actions)
        self.update_buttons()
        self.run(self.initial_data, self.initial_loaded)

    def initial_data(self, progress):
        return self.directory.load(), self.directory.suppliers()

    def initial_loaded(self, result):
        self.data, suppliers = result
        for warehouse, value in suppliers.items():
            self.supplier_edits[warehouse].setText(value)
        self.fill_directory()

    def on_idle(self):
        if self.detect_pending:
            self.detect_pending = False
            self.detect()
            return
        if self.cancel_pending:
            self.cancel_pending = False
            self.stop_setup()

    def import_directory(self):
        from import_ui import choose
        paths = choose(self, 'aoxiang_stores', '导入翱象门店目录')
        if not paths:
            return
        def operation(progress):
            data = read_directory(paths[0], progress=progress)
            return self.directory.save(data, progress=progress)
        self.run(operation, self.directory_loaded)

    def directory_loaded(self, data):
        self.data = data
        self.fill_directory()
        self.status.setText(f'已保存 {len(data["items"])} 家门店；勾选要核验的门店。原文件已留存。')

    def download_template(self):
        from import_ui import download
        download(self, 'aoxiang_stores')

    def fill_directory(self):
        items = self.data['items'] if self.data else []
        if self.data:
            self.note.setText(f'门店目录 {len(items)} 家 · ' + self.data.get('source', {}).get('name', '已保存目录') + '；名称相同也按编码区分。')
        self.table.setRowCount(len(items))
        self.checks = []
        for row, item in enumerate(items):
            check = QCheckBox()
            check.toggled.connect(self.update_buttons)
            self.checks.append(check)
            self.table.setCellWidget(row, 0, check)
            for col, value in [(1, item['name']), (2, item['code']), (3, '尚未核验')]:
                self.table.setItem(row, col, QTableWidgetItem(value))
        self.filter_rows()

    def filter_rows(self, *_):
        search = self.search.text().casefold().strip()
        for row, item in enumerate((self.data or {}).get('items', [])):
            self.table.setRowHidden(row, search not in (item['name'] + ' ' + item['code']).casefold())

    def select_visible(self, value):
        for row, check in enumerate(self.checks):
            if not self.table.isRowHidden(row):
                check.setChecked(value)

    def clear_selection(self):
        for check in self.checks:
            check.setChecked(False)

    def selected(self):
        return [{'code': item['code'], 'name': item['name']} for item, check in zip((self.data or {}).get('items', []), self.checks) if check.isChecked()]

    def detect(self):
        self.status.setText('正在检测本机 Edge 与已有用户配置…')
        self.progress.setRange(0, 0)
        def operation(progress):
            self.ensure_client()
            return self.client.request('discover'), self.client.request('configuration')
        self.run(operation, self.discovered)

    def discovered(self, result):
        found, configured = result
        self.edge.clear()
        self.profile.clear()
        for item in found['installations']:
            self.edge.addItem(item['executable'] + ' · ' + item['version'], item['executable'])
        for item in found['profiles']:
            self.profile.addItem(item['displayName'] + ' · ' + item['profileDirectory'], item['actualProfilePath'])
        verified = {item['buyerWarehouseCode']: item for item in configured.get('targets', [])}
        for row, item in enumerate((self.data or {}).get('items', [])):
            old = verified.get(item['code'])
            if old and old['displayName'] == item['name']:
                self.table.item(row, 3).setText('已保存核验 · ' + old['mappingVerifiedAt'])
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setFormat('浏览器检测完成；门店核验尚未开始')
        self.status.setText('请先勾选门店，再核验登录。已找到浏览器不代表已经连接或登录；Edge 弹出调试连接请求时需允许。')

    def verify(self):
        targets = self.selected()
        if not targets or not self.edge.currentData() or not self.profile.currentData():
            return
        params = {'candidate': {'edge_executable': self.edge.currentData(), 'actual_profile_path': self.profile.currentData()},
                  'output_root': self.output.text().strip(), 'targets': targets}
        self.status.setText(f'准备核验 {len(targets)} 家门店…')
        self.transport_failed = False
        self.last_status = None
        self.progress.setFormat('%v / %m 家已处理')
        self.progress.setRange(0, 0)
        self.run(lambda _: self.client.request('setup.start', **params), self.setup_updated)

    def setup_updated(self, data):
        self.transport_failed = False
        self.last_status = data
        self.status_received = time.monotonic()
        self.setup_id, self.setup_state = data['id'], data['state']
        self.setup_cancel_requested = data.get('cancel_requested', False)
        states = {'PENDING': '等待核验', 'RUNNING': '正在核验', 'VERIFIED': '编码一致', 'FAILED': '核验未通过'}
        by_code = {item['code']: row for row, item in enumerate((self.data or {}).get('items', []))}
        for item in data['targets']:
            if item['code'] in by_code:
                text = states[item['state']] + (' · ' + item['error']['message'] if item.get('error') else '')
                self.table.item(by_code[item['code']], 3).setText(text)
        self.progress.setRange(0, max(data['total'], 1) if data['current'] or data['state'] != 'RUNNING' else 0)
        self.progress.setValue(data['current'])
        identity = data.get('identity') or {}
        phase = {'CONNECTING': '正在连接 Edge，必要时请允许调试连接', 'PROFILE_VERIFIED': '已核对浏览器用户配置'}.get(data['stage'], data['stage'])
        if self.setup_cancel_requested and data['state'] == 'RUNNING':
            phase = '已请求停止，等待当前网页查询安全结束；未保存新配置'
        self.base_status = (identity.get('display_name', '') + ' ' + identity.get('merchant_code', '') + ' · ' if identity else '') + phase + (' · ' + data['error']['code'] + '：' + data['error']['message'] if data.get('error') else '')
        self.status.setText(self.base_status)
        if data['state'] == 'RUNNING' or data.get('cleanup_pending'):
            self.poll.start()
            self.clock.start()
        else:
            self.poll.stop()
            self.clock.stop()
        if data['state'] == 'READY':
            self.status.setText(self.status.text() + f'；通过 {data["verified"]} 家，失败 {data["failed"]} 家。只保存通过项。')
        self.update_buttons()

    def render_status(self):
        if self._closing or self.transport_failed or not self.last_status or self.last_status['state'] != 'RUNNING':
            return
        from datetime import datetime, timezone
        started = self.last_status.get('stage_started_at')
        elapsed = max(0, int((datetime.now(timezone.utc) - datetime.fromisoformat(started.replace('Z', '+00:00'))).total_seconds())) if started else 0
        limit = self.last_status.get('operation_timeout_ms', 0) // 1000
        self.status.setText(self.base_status + (f' · 当前步骤 {elapsed} 秒 / 最长 {limit} 秒' if limit else '') +
                            (' · 状态应答等待中，可停止或关闭' if time.monotonic() - self.status_received > 5 else ''))

    def failure(self, text):
        # A failed status request must not open a new modal every 600 ms.
        self.poll.stop()
        self.clock.stop()
        self.transport_failed = True
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setFormat('核验未完成')
        super().failure(text + '\n可点击“异常后重新连接”结束本次只读核验，再重新开始；不会保存未通过配置。')

    def reconnect(self):
        if self.worker or self._closing:
            return
        self.reconnecting = True
        self.poll.stop()
        self.clock.stop()
        self.status.setText('正在结束原只读核验并释放连接；保留已保存配置，不关闭你的 Edge。')
        def finished(done):
            if not done:
                QTimer.singleShot(600, self.reconnect)
                return
            if not hasattr(self.client, 'reset'):
                self.client = self.client_factory(component_root(), self.folder / 'component-state')
            self.started = False
            self.setup_id = self.setup_state = self.last_status = None
            self.setup_cancel_requested = self.cancel_pending = False
            self.transport_failed = self.reconnecting = False
            self.detect_pending = True
        self.run(lambda _: getattr(self.client, 'reset', self.client.close)(timeout=.5), finished)

    def refresh_setup(self):
        if self.worker or self._closing or not self.setup_id:
            return
        setup_id = self.setup_id
        self.run(lambda _: self.client.request('setup.status', setup_id=setup_id), self.setup_updated)

    def stop_setup(self):
        if not self.setup_id:
            return
        if self.worker:
            self.cancel_pending = True
            return
        self.status.setText('已请求停止核验，等待当前网页查询安全结束；尚未保存新配置。')
        setup_id = self.setup_id
        self.run(lambda _: self.client.request('setup.cancel', setup_id=setup_id), self.setup_updated)

    def save_setup(self):
        if self.setup_state != 'READY':
            return
        setup_id = self.setup_id
        self.run(lambda _: self.client.request('setup.commit', setup_id=setup_id), self.setup_updated)

    def save_suppliers(self):
        values = {name: edit.text().strip() for name, edit in self.supplier_edits.items()}
        self.run(lambda _: self.directory.save_suppliers(values), lambda _: self.status.setText('两仓供应商对应已保存；不会改变历史订单。'))

    def update_buttons(self, *_):
        available = self.worker is None and not self._closing and not self.reconnecting
        setting = self.setup_state in ('RUNNING', 'READY', 'COMMITTING') or bool((self.last_status or {}).get('cleanup_pending'))
        self.selection_count.setText(f'已选 {len(self.selected())} 家')
        for widget in [self.import_btn, self.template_btn, self.detect_btn, self.edge, self.profile, self.output, self.supplier_btn, self.table, self.all_btn, self.none_btn, *self.supplier_edits.values()]:
            widget.setEnabled(available and not setting)
        self.verify_btn.setEnabled(available and not setting and bool(self.selected()) and bool(self.edge.currentData()) and bool(self.profile.currentData()))
        self.save_btn.setEnabled(available and self.setup_state == 'READY' and not self.transport_failed and not (self.last_status or {}).get('cleanup_pending'))
        self.reconnect_btn.setEnabled(available and self.transport_failed)
        self.stop_btn.setEnabled(not self._closing and not self.setup_cancel_requested and self.setup_state in ('RUNNING', 'READY'))
        self.close_btn.setEnabled(not self._closing)


def open_setup(host):
    if host.worker or getattr(host, 'readonly', False):
        return
    dialog = SetupDialog(host, host.repository.folder)
    show_modal(host, dialog)
