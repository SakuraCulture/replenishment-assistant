"""Focused, local-first receipt status and safe native-file synchronization."""
import json
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QVBoxLayout, QHBoxLayout, QComboBox, QLineEdit, QProgressBar, QHeaderView
from aoxiang_client import ProcessClient
from aoxiang_directory import Directory
from aoxiang_receipts import checked_json, synchronize
from aoxiang_purchase_tasks import inspect_basis
from aoxiang_ui_common import ComponentDialog, component_root, show_modal
from business_ui import grid
from ui_controls import button, label

STATES = {'IDLE': '等待下一次查询', 'RUNNING': '正在同步平台资料', 'PAUSED': '已停止，可继续原任务',
          'NEEDS_REVIEW': '资料待核或下载未完成', 'READY': '完整终态已验收，待保存正式实收', 'SYNCED': '正式实收已保存'}
PHASES = {'PURCHASE_RUNNING': '核对指定采购单查询范围', 'INTENT_RECORDED': '记录原生导出任务',
          'ACCEPTED': '平台已受理导出，等待生成', 'SAVED': '原文件已下载并校验',
          'PURCHASE_COMPLETE': '主单与明细已保存', 'PURCHASE_EMPTY': '未查到指定采购单'}


def display_time(value):
    if not value:
        return '—'
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            return value
        return stamp.astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError):
        return str(value)


class ReceiptDialog(ComponentDialog):
    def __init__(self, parent, host, head, *, client_factory=ProcessClient):
        super().__init__(parent)
        self.host, self.head = host, head
        self.folder = Path(host.repository.folder) / 'aoxiang'
        self.client_factory = client_factory
        self.client = client_factory(component_root(), self.folder / 'component-state')
        self.started = False
        self.worker = None
        self._closing = self._can_close = self._close_done = False
        self.records, self.current = [], None
        self.configuration = self.suppliers = None
        self.pending_save = self.local_saving = False
        self.local_cancel = threading.Event()
        self.poll = QTimer(self)
        self.poll.setInterval(1000)
        self.poll.timeout.connect(self.refresh_current)
        self.setWindowTitle('翱象收货 · ' + head['number'])
        self.resize(1080, 740)
        v = QVBoxLayout(self)
        v.addWidget(label(head['name'] + ' · ' + head['warehouse'] + ' · ' + head['number'], 'section'))
        v.addWidget(label('按已关联采购单同步。已完成即停止常规监控；实收完整校验后自动保存，原付款和签发不变。', 'muted'))
        self.summary = label('正在读取本地已关联采购单…')
        self.summary.setWordWrap(True)
        v.addWidget(self.summary)
        row = QHBoxLayout()
        self.orders = QComboBox()
        self.orders.currentIndexChanged.connect(self.select_record)
        row.addWidget(self.orders, 1)
        self.reload_btn = button('刷新本地记录', self.load)
        row.addWidget(self.reload_btn)
        v.addLayout(row)
        search = QLineEdit()
        search.setPlaceholderText('筛选商品编码、名称或待核原因')
        v.addWidget(search)
        self.table, self.model = grid(self, ['商品编码', '商品名称', '基础单位', '采购数量', '实际入库数量', '数量差额', '入库金额（元）', '待核原因'])
        for column, width in enumerate((180, 140, 65, 80, 105, 80, 110)):
            self.table.setColumnWidth(column, width)
        self.table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        search.textChanged.connect(self.model.filter)
        v.addWidget(self.table, 1)
        self.note = label('打开本页只读取已保存记录；点击立即同步后才查询平台。空白实收显示“未知”，不当作0。')
        self.note.setWordWrap(True)
        v.addWidget(self.note)
        self.basis_note = label('尚未选择采购记录。', 'muted')
        self.basis_note.setWordWrap(True)
        v.addWidget(self.basis_note)
        row = QHBoxLayout()
        self.sync_btn = button('立即同步 / 继续原下载任务', self.start_sync, True)
        self.save_btn = button('重试保存已验收实收', self.retry_save)
        self.stop_btn = button('停止后续动作', self.stop)
        for b in (self.sync_btn, self.save_btn, self.stop_btn, button('关闭', self.close)):
            row.addWidget(b)
        v.addLayout(row)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1)
        v.addWidget(self.bar)
        self.status = label('读取本地记录中…', 'muted')
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        self.update_buttons()
        QTimer.singleShot(0, self.load)

    def update_buttons(self):
        idle = not self.worker and not self._closing
        active = bool((self.current or {}).get('active'))
        state = (self.current or {}).get('state')
        self.sync_btn.setEnabled(idle and not active and bool(self.current) and state not in ('READY', 'SYNCED') and not self.head.get('deleted'))
        self.save_btn.setEnabled(idle and not active and state == 'READY' and not self.head.get('deleted'))
        self.stop_btn.setEnabled((idle and active) or (self.local_saving and not self.local_cancel.is_set()))
        self.reload_btn.setEnabled(idle and not active)
        self.orders.setEnabled(idle and not active)
        self.bar.setRange(0, 0 if self.worker or active else 1)
        if not self.worker and not active:
            self.bar.setValue(1 if state in ('READY', 'SYNCED') else 0)
            self.bar.setFormat(STATES.get(state, '尚无同步任务'))

    def load(self):
        def operation(_):
            self.ensure_client()
            configuration = self.client.request('configuration')
            records, offset = [], 0
            while True:
                page = self.client.request('purchase.list', offset=offset, limit=100)
                records.extend(r for r in page['items'] if r['state'] == 'BOUND' and all(r['basis'][k] == self.head[k] for k in ('number', 'store', 'warehouse')))
                offset += len(page['items'])
                if offset >= page['total'] or not page['items']:
                    break
            return configuration, Directory(self.folder).suppliers(), records
        self.run(operation, self.loaded)

    def loaded(self, data):
        self.configuration, self.suppliers, self.records = data
        self.orders.blockSignals(True)
        self.orders.clear()
        for r in self.records:
            self.orders.addItem('采购单 ' + r['binding']['orderId'], r['id'])
        self.orders.setCurrentIndex(0)
        self.orders.blockSignals(False)
        self.current = None
        self.basis_note.setText('正在核对原采购依据与当前付款…' if self.records else '尚无已关联采购记录。')
        self.model.replace([])
        if self.records:
            # load's worker has not finished yet; defer the selected status call to on_idle.
            self.pending_read = self.records[0]['id']
        else:
            self.summary.setText('本单尚无已核验关联的翱象采购单。请先在翱象采购中核对原任务。')
            self.status.setText('未连接平台。')

    def select_record(self, index):
        if 0 <= index < len(self.records):
            self.current = None
            self.basis_note.setText('正在核对原采购依据与当前付款…')
            self.pending_read = self.records[index]['id']
            self.on_idle()

    def status_data(self, id, *, check_basis=False):
        record = self.client.request('receipt.status', purchase_id=id)
        observation = checked_json(record['manifest']['file'], record['manifest']['sha256']) if record.get('manifest') else None
        state = None
        if check_basis:
            context = self.folder / 'purchase-packages' / id / '本地任务索引.json'
            try:
                package = json.loads(context.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                package = None
            state = inspect_basis(self.host.work_repository.path, package, self.client.request('configuration'), Directory(self.folder).suppliers())
        return record, observation, state

    def on_idle(self):
        if self.worker or self._closing:
            return
        id = getattr(self, 'pending_read', None)
        if id:
            self.pending_read = None
            self.run(lambda _: self.status_data(id, check_basis=True), self.render)
        elif self.pending_save:
            self.pending_save = False
            self.save_receipt()

    def render(self, data):
        record, o = data[:2]
        if record['id'] != self.orders.currentData() or self.current and record['sequence'] < self.current['sequence']:
            return
        self.current = record
        if len(data)>2 and data[2] is not None:
            self.basis_note.setText(data[2]['message'])
        summary = record.get('summary') or {}
        self.summary.setText('采购单 ' + record['scope']['orderId'] + ' · ' + STATES.get(record['state'], record['state']) +
                             '\n平台状态：' + summary.get('status', '尚未查询') + ' · 最后观察：' + display_time(summary.get('observedAt')))
        note = '已见“已完成”：常规监控已停止；最终实收任务单独保留。' if record['terminal'] else '尚未见“已完成”；已知入库值仅作进度观察，不提前记账。'
        if record.get('error'):
            note += '\n' + record['error']['message']
        if o and o['issues']:
            note += '\n' + '；'.join(dict.fromkeys(i['message'] for i in o['issues']))
        self.note.setText(note)
        rows = []
        for r in o['rows'] if o else []:
            known = r['receivedQuantity'] not in ('', None)
            try:
                delta = str(Decimal(r['receivedQuantity']) - Decimal(r['quantity'])) if known else '未知'
            except ArithmeticError:
                delta = '待核'
            rows.append([r['sku'], r['name'], r['unit'], r['quantity'], r['receivedQuantity'] if known else '未知', delta,
                         r['receivedAmount'] if r['receivedAmount'] not in ('', None) else '未知',
                         '；'.join(i['message'] for i in o['issues'] if i.get('sku') in (None, r['sku']))])
        self.model.replace(rows)
        stage = record.get('stage')
        self.status.setText(PHASES.get(stage, stage) or STATES.get(record['state'], record['state']))
        if record.get('active'):
            self.poll.start()
        else:
            self.poll.stop()
        self.update_buttons()

    def refresh_current(self):
        if self.worker or self._closing or not self.current:
            return
        id = self.current['id']
        def received(data):
            self.render(data)
            if self.current and self.current['state'] == 'READY' and not self._closing:
                self.pending_save = True
        self.run(lambda _: self.status_data(id), received)

    def start_sync(self):
        id = self.current['id']
        def started(r):
            self.render((r, None))
            if r['state'] == 'READY':
                self.pending_save = True
        self.run(lambda _: self.client.request('receipt.start', purchase_id=id), started)

    def retry_save(self):
        self.pending_save = True
        self.on_idle()

    def save_receipt(self):
        id, manifest = self.current['id'], self.current['manifest']
        self.local_cancel = threading.Event()
        self.local_saving = True
        def operation(progress):
            context = self.folder / 'purchase-packages' / id / '本地任务索引.json'
            if not context.is_file():
                raise ValueError('原采购付款依据未找到；保留终态及原件，不能猜测写入实收')
            package = json.loads(context.read_text(encoding='utf-8'))
            config = self.client.request('configuration')
            receipt = synchronize(self.host.work_repository.path, package, manifest, config, Directory(self.folder).suppliers(), cancel=self.local_cancel, progress=progress)
            progress('正式实收已保存，登记同步回执；若回执中断，重试不会新增实收版本。')
            self.client.request('receipt.synced', purchase_id=id, manifest_sha256=manifest['sha256'], receipt=receipt)
            return self.status_data(id)
        def saved(data):
            self.local_saving = False
            self.render(data)
        self.run(operation, saved)

    def stop(self):
        if self.local_saving:
            self.local_cancel.set()
            self.status.setText('已请求停止。若已进入最终提交，等待提交结束；不强杀、不撤销已保存实收。')
            self.update_buttons()
        elif self.current:
            id = self.current['id']
            self.run(lambda _: self.client.request('receipt.pause', purchase_id=id), lambda r: self.render((r, None)))

    def failure(self, text):
        self.local_saving = self.pending_save = False
        self.poll.stop()
        if self.current:
            self.current = {**self.current, 'active': False}
        super().failure(text)


def open_receipts(parent, host, head):
    return show_modal(host, ReceiptDialog(parent, host, head))
