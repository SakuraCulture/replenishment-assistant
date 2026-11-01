"""Focused confirmed-payment -> AX purchase preview, creation and exact-task recovery."""
import json
import time
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QVBoxLayout, QHBoxLayout, QComboBox, QLabel, QProgressBar,
                             QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QMessageBox)
from aoxiang_client import ProcessClient
from aoxiang_directory import Directory, atomic_json
from aoxiang_purchase_tasks import prepare, validate_for_upload
from aoxiang_ui_common import ComponentDialog, component_root, show_modal
from ui_controls import label, button

STATES = {'RESERVED': '已准备，尚未上传', 'PREPARING': '核验上传准备', 'READY': '准备就绪',
          'SUBMITTING': '正在上传，等待唯一回执', 'ACCEPTED': '已受理，待核对原任务', 'VERIFYING': '正在核对采购单',
          'BOUND': '已关联采购单 · 待提交', 'PAUSED': '已停止，尚未上传', 'FAILED_PRECHECK': '准备未通过，尚未上传',
          'NEEDS_REVIEW': '上传结果待核，禁止重传', 'CANCELLED_BEFORE_UPLOAD': '已在上传前取消'}


class PurchaseDialog(ComponentDialog):
    def __init__(self, parent, host, head, *, client_factory=ProcessClient):
        super().__init__(parent)
        self.host, self.head = host, head
        self.folder = Path(host.repository.folder) / 'aoxiang'
        self.client_factory = client_factory
        self.client = client_factory(component_root(), self.folder / 'component-state')
        self.started = False
        self.worker = None
        self._closing = self._can_close = self._close_done = False
        self.configuration = self.suppliers = self.receipt = self.package = self.current = None
        self.template_id = None
        self.make_preview_pending = False
        self.records = []
        self.began = time.monotonic()
        self.poll = QTimer(self)
        self.poll.setInterval(900)
        self.poll.timeout.connect(self.refresh_current)
        self.setWindowTitle('翱象采购 · ' + head['number'])
        self.resize(1080, 760)
        layout = QVBoxLayout(self)
        layout.addWidget(label(head['name'] + ' · ' + head['warehouse'] + ' · ' + head['number'], 'section'))
        layout.addWidget(label('使用已确认付款数量和金额生成采购单；仅创建为“待提交”，后续提交由业务在翱象操作。', 'muted'))
        self.summary = label('正在读取本单采购记录…')
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        row = QHBoxLayout()
        self.history = QComboBox()
        self.history.currentIndexChanged.connect(self.select_record)
        row.addWidget(QLabel('本单采购记录'))
        row.addWidget(self.history, 1)
        self.reload_btn = button('刷新本单记录', self.load)
        row.addWidget(self.reload_btn)
        layout.addLayout(row)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['商品编码', '商品名称', '付款基础数量', '付款商品金额（元）'])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 220)
        self.table.setColumnWidth(2, 170)
        self.table.setColumnWidth(3, 190)
        layout.addWidget(self.table, 1)
        self.note = label('预览逐项来自本单付款快照，不以最新商品价格替换。')
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        row = QHBoxLayout()
        self.preview_btn = button('生成采购预览（获取最新模板）', self.fetch_template)
        self.upload_btn = button('上传建单（保留待提交）', self.upload, True)
        self.verify_btn = button('继续核对原采购任务', self.verify)
        for b in (self.preview_btn, self.upload_btn, self.verify_btn):
            row.addWidget(b)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.stop_btn = button('停止后续动作', self.stop)
        self.cancel_btn = button('取消尚未上传的任务', self.cancel_reserved)
        self.close_btn = button('关闭', self.close)
        for b in (self.stop_btn, self.cancel_btn, self.close_btn):
            row.addWidget(b)
        layout.addLayout(row)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        layout.addWidget(self.bar)
        self.status = label('正在读取本地配置…', 'muted')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.update_buttons()
        QTimer.singleShot(0, self.load)

    def update_buttons(self):
        idle = not self.worker and not self._closing
        active = bool(self.template_id or (self.current or {}).get('active'))
        available = bool((self.configuration or {}).get('enabled'))
        pending = any(r['state'] != 'CANCELLED_BEFORE_UPLOAD' for r in self.records)
        attempted = bool((self.current or {}).get('uploadAttempted'))
        self.preview_btn.setEnabled(idle and not active and available and not pending and not self.head.get('deleted'))
        self.upload_btn.setEnabled(idle and not active and available and bool(self.package) and not attempted
                                   and (self.current or {}).get('state') != 'CANCELLED_BEFORE_UPLOAD' and not self.head.get('deleted'))
        self.verify_btn.setEnabled(idle and not active and bool((self.current or {}).get('taskId'))
                                   and (self.current or {}).get('state') != 'BOUND' and not (self.current or {}).get('additionalTaskIds'))
        self.stop_btn.setEnabled(idle and active)
        self.cancel_btn.setEnabled(idle and not active and bool(self.current) and not attempted
                                  and self.current['state'] in ('RESERVED', 'PAUSED', 'FAILED_PRECHECK'))
        self.reload_btn.setEnabled(idle and not active)
        self.history.setEnabled(idle and not active)
        self.bar.setRange(0, 0 if self.worker or active else 1)
        if not self.worker and not active:
            self.bar.setValue(1 if (self.current or {}).get('state') == 'BOUND' or self.package else 0)

    def load(self):
        def operation(_):
            self.ensure_client()
            config = self.client.request('configuration')
            rows, offset = [], 0
            while True:
                page = self.client.request('purchase.list', offset=offset, limit=100)
                rows.extend(r for r in page['items'] if r['basis']['number'] == self.head['number']
                            and r['basis']['warehouse'] == self.head['warehouse'] and r['basis']['store'] == self.head['store'])
                offset += len(page['items'])
                if offset >= page['total'] or not page['items']:
                    break
            return config, Directory(self.folder).suppliers(), rows
        self.run(operation, self.loaded)

    def loaded(self, data):
        self.configuration, self.suppliers, self.records = data
        self.history.blockSignals(True)
        self.history.clear()
        for r in self.records:
            self.history.addItem(r['createdAt'] + ' · ' + STATES.get(r['state'], r['state']), r['id'])
        self.history.setCurrentIndex(len(self.records) - 1)
        self.history.blockSignals(False)
        self.select_record(len(self.records) - 1)
        if not self.records:
            self.summary.setText('本单尚未创建翱象采购任务。' + ('' if self.configuration.get('enabled') else ' 请先在准备数据页配置并核验翱象连接。'))
        self.status.setText('本地记录已读取；生成预览会获取最新原生模板，预览本身不会创建采购单。')

    def failure(self, text):
        self.poll.stop()
        self.template_id = None
        self.make_preview_pending = False
        if self.current:
            self.current = {**self.current, 'active': False}
        super().failure(text)

    def select_record(self, index):
        if not 0 <= index < len(self.records):
            self.current = None
            self.package = None
            return
        self.current = self.records[index]
        context = self.folder / 'purchase-packages' / self.current['id'] / '本地任务索引.json'
        try:
            self.package = json.loads(context.read_text(encoding='utf-8')) if context.is_file() else None
        except (OSError, ValueError):
            self.package = None
        self.render()

    def render(self):
        if self.package:
            p = self.package['intent']
            self.summary.setText(f"{p['target']['displayName']} · {self.head['warehouse']} · 供应商 {p['supplierCode']}\n"
                                 f"{len(p['rows'])} 款 · 基础数量 {p['totalQuantity']} · 付款金额 {p['totalAmount']} 元")
            self.table.setRowCount(len(p['rows']))
            for i, r in enumerate(p['rows']):
                for j, k in enumerate(('sku', 'name', 'quantity', 'amount')):
                    self.table.setItem(i, j, QTableWidgetItem(str(r[k])))
        else:
            self.table.setRowCount(0)
            if self.current:
                self.summary.setText(f"{self.current['rows']} 款 · 数量 {self.current['totalQuantity']} · 金额 {self.current['totalAmount']} 元；本地预览文件未找到，原平台任务仍保留。")
        if self.current:
            r = self.current
            text = STATES.get(r['state'], r['state'])
            if r.get('binding'):
                text += ' · 采购单号：' + r['binding']['orderId']
            elif r.get('taskId'):
                text += '\n原导入任务号：' + r['taskId']
            if r.get('error'):
                text += '\n' + r['error']['message']
            self.note.setText(text)
        self.update_buttons()

    def fetch_template(self):
        self.began = time.monotonic()
        self.run(lambda _: self.client.request('template.start'), self.template_started)

    def template_started(self, result):
        self.template_id = result['id']
        self.poll.start()
        self.status.setText('正在获取最新采购模板；尚未创建采购单。')

    def refresh_current(self):
        if self.worker or self._closing:
            return
        if self.template_id:
            expected = self.template_id
            self.run(lambda _: self.client.request('template.status', template_id=expected), self.template_status)
        elif self.current and self.current.get('active'):
            expected = self.current['id']
            self.run(lambda _: self.client.request('purchase.status', purchase_id=expected), self.purchase_status)

    def template_status(self, result):
        if result['id'] != self.template_id:
            return
        self.status.setText(result['stage'] + f" · 已等待 {int(time.monotonic()-self.began)} 秒")
        if result['state'] != 'RUNNING':
            self.template_id = None
            self.poll.stop()
            if result['state'] == 'READY':
                self.receipt = result['receipt']
                self.make_preview_pending = True
            elif result.get('error'):
                self.failure(result['error']['message'])

    def on_idle(self):
        if self.make_preview_pending:
            self.make_preview_pending = False
            self.run(self.prepare_package, self.package_ready)

    def prepare_package(self, progress):
        config = self.client.request('configuration')
        suppliers = Directory(self.folder).suppliers()
        p = prepare(self.host.work_repository.path, self.head, config, suppliers, self.receipt,
                    self.folder / 'purchase-packages', component_root() / 'assets/purchase-import-template-contract.json', progress=progress)
        atomic_json(Path(p['package_file']).parent / '本地任务索引.json', p)
        registered = self.client.request('purchase.reserve', package_file=p['package_file'], package_sha256=p['package_sha256'])
        return p, registered['record']

    def package_ready(self, data):
        p, record = data
        self.package = p
        self.current = record
        self.records.append(record)
        self.history.blockSignals(True)
        self.history.addItem(record['createdAt'] + ' · ' + STATES.get(record['state'], record['state']), record['id'])
        self.history.setCurrentIndex(self.history.count() - 1)
        self.history.blockSignals(False)
        self.render()
        self.note.setText('请核对商品、数量、金额及供应商；尚未上传，也未创建任何平台采购单。')
        self.status.setText('采购预览已生成，确认后再上传建单。')

    def upload(self):
        p = self.package['intent']
        answer = QMessageBox.question(self, '确认上传并创建采购单',
            f"{p['target']['displayName']} · {self.head['warehouse']}\n供应商编码：{p['supplierCode']}\n"
            f"{len(p['rows'])} 款，基础数量 {p['totalQuantity']}，付款金额 {p['totalAmount']} 元。\n创建后保留待提交，不自动提交或入库。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.began = time.monotonic()
        def operation(_):
            validate_for_upload(self.host.work_repository.path, self.package, self.client.request('configuration'), Directory(self.folder).suppliers())
            r = self.client.request('purchase.reserve', package_file=self.package['package_file'], package_sha256=self.package['package_sha256'])
            return self.client.request('purchase.start', purchase_id=r['record']['id'])
        self.run(operation, self.purchase_status)

    def purchase_status(self, result):
        if self.current and result['id'] != self.current['id']:
            return
        if self.current and result['sequence'] < self.current['sequence']:
            return
        self.current = result
        if any(r['id'] == result['id'] for r in self.records):
            self.records = [result if r['id'] == result['id'] else r for r in self.records]
        else:
            self.records.append(result)
        for i in range(self.history.count()):
            if self.history.itemData(i) == result['id']:
                self.history.setItemText(i, result['createdAt'] + ' · ' + STATES.get(result['state'], result['state']))
        self.render()
        self.status.setText((result.get('stage') or STATES.get(result['state'], result['state'])) + f" · 已等待 {int(time.monotonic()-self.began)} 秒")
        if result.get('active'):
            self.poll.start()
        else:
            self.poll.stop()

    def verify(self):
        self.began = time.monotonic()
        self.run(lambda _: self.client.request('purchase.start', purchase_id=self.current['id']), self.purchase_status)

    def stop(self):
        if self.template_id:
            self.run(lambda _: self.client.request('template.cancel', template_id=self.template_id), self.template_status)
        elif self.current:
            self.run(lambda _: self.client.request('purchase.pause', purchase_id=self.current['id']), self.purchase_status)

    def cancel_reserved(self):
        if QMessageBox.question(self, '取消尚未上传的任务', '确认取消此任务？仅允许取消尚未进入上传的任务，平台采购单不会被删除。',
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.run(lambda _: self.client.request('purchase.cancel', purchase_id=self.current['id']), self.purchase_status)


def open_purchase(parent, host, head):
    dialog = PurchaseDialog(parent, host, head)
    show_modal(host, dialog)
