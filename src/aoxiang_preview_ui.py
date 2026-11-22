"""Confirm the exact paid warehouse-order scope for an AX preview."""

from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout

from aoxiang_preview_scope import MAX_ORDERS, enqueue, inspect_scope
from business_ui import run
from runtime_support import Cancelled
from ui_controls import button, label


class PreviewScope(QDialog):
    def __init__(self, parent, host, heads, *, saved=False):
        super().__init__(parent)
        self.host = host
        self.manager = host.aoxiang_workflows
        self.heads = list(heads)
        self.saved = saved
        self.ready_heads = []
        self.busy = False
        self.created_job = None
        self.setObjectName('aoxiangPreviewScope')
        self.setWindowTitle('付款保存结果' if saved else '核对翱象采购预览范围')
        self.resize(1120, 560)
        layout = QVBoxLayout(self)
        subject = '本次付款核对并保存' if saved else '本次选择'
        self.summary = label(f'{subject} {len(self.heads)} 张仓单 · 正在核对采购范围', 'section')
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(['门店', '仓', '补货单号', '付款版本', '处理提示', '原任务'])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for i, width in enumerate((182, 75, 255, 86, 310, 139)):
            self.table.setColumnWidth(i, width)
        layout.addWidget(self.table, 1)
        self.note = QLabel('生成采购预览后，核对数量和金额再确认上传。')
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        actions = QHBoxLayout()
        actions.addWidget(button('关闭', self.reject))
        self.reload_btn = button('重新核对范围', self.refresh)
        actions.addWidget(self.reload_btn)
        self.prepare_btn = button('生成翱象采购预览', self.prepare, True)
        self.prepare_btn.setEnabled(False)
        actions.addWidget(self.prepare_btn)
        layout.addLayout(actions)

    def refresh(self):
        if self.busy or self.host.closing:
            return
        self.prepare_btn.setEnabled(False)
        self.reload_btn.setEnabled(False)
        self.busy = True
        try:
            store = self.manager.store
            rows = run(self, inspect_scope, (self.host.work_repository.path, store.folder, self.heads), '核对付款和原采购记录')
            if not self.host.closing:
                self.show_rows(rows)
        except Cancelled:
            self.note.setText('已取消核对，可重新核对范围。')
        except Exception as exc:
            self.note.setText('采购范围核对未完成，可重新核对。'+str(exc))
        finally:
            self.busy = False
            self.reload_btn.setEnabled(True)

    def show_rows(self, rows):
        self.ready_heads = [{**r['head'], 'preview_payment': r['payment']} for r in rows if r['state'] == 'ready']
        n = len(self.ready_heads)
        prefix = f'本次付款核对并保存 {len(rows)} 张仓单' if self.saved else f'本次选择 {len(rows)} 张仓单'
        self.summary.setText(f'{prefix} · 可生成采购预览 {n} 单 · 其余 {len(rows)-n} 单见处理提示')
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            head = row['head']
            payment = row.get('payment') or head.get('preview_payment')
            version = f"V{payment['version']:03d}" if payment else '—'
            values = (head.get('name', head['store']), head['warehouse'], head['number'], version, row['message'])
            for col, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setToolTip(value)
                self.table.setItem(i, col, cell)
            self.table.removeCellWidget(i, 5)
            if row.get('batch') or row.get('purchase_id'):
                title = '打开原批次' if row.get('batch') else '查看原采购'
                self.table.setCellWidget(i, 5, button(title, lambda checked=False, record=row: self.original(record)))
        title = '继续生成翱象采购预览' if self.saved else '生成翱象采购预览'
        self.prepare_btn.setText(f'{title}（{n} 单）')
        self.prepare_btn.setEnabled(0 < n <= MAX_ORDERS)
        self.note.setText(
            f'每批最多 {MAX_ORDERS} 张仓单，请从付款核对页缩小范围后分批处理。' if n > MAX_ORDERS
            else '生成采购预览后，核对数量和金额再确认上传。')

    def original(self, row):
        if self.busy or self.host.closing:
            return
        if row.get('batch'):
            self.manager.open_queue(row['batch'])
        else:
            from aoxiang_purchase_ui import open_purchase
            open_purchase(self, self.host, row['head'], original_id=row['purchase_id'])

    def prepare(self):
        if self.busy or self.host.closing or not 0 < len(self.ready_heads) <= MAX_ORDERS:
            return
        self.busy = True
        self.prepare_btn.setEnabled(False)
        self.reload_btn.setEnabled(False)
        try:
            self.manager.ensure_owner()
            self.created_job = run(self, enqueue, (self.manager.store, self.ready_heads), '按所选仓单准备采购预览')
            self.accept()
        except Cancelled:
            self.note.setText('已取消准备，可重新核对范围。')
        except Exception as exc:
            self.note.setText(str(exc))
        finally:
            self.busy = False
            self.reload_btn.setEnabled(True)

    def reject(self):
        if not self.busy:
            super().reject()
