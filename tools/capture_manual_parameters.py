"""Capture real Qt forms with synthetic, isolated data; never start platform clients.

The separate parameter workspace runner supplies frozen EXE screenshots. These
additional captures are explicitly labelled as source Qt in the manual index.
"""
import argparse
import copy
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--out', type=Path, required=True)
    args = p.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    os.environ['REPLENISHMENT_DATA_DIR'] = str(out / 'settings')
    os.environ.pop('QT_QPA_PLATFORM', None)
    sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QFont, QColor, QPalette
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QWidget, QLineEdit, QDialog, QTableView
    from ui_controls import STYLE
    from work_repository import Repository
    import parameter_schema as schema
    import parameter_guidance as guidance
    import parameter_tables
    import store_parameter_import as spi
    import store_parameter_ui as sui
    from parameter_maintenance_ui import PatchDialog
    from parameter_ui import ParameterDialog
    from aoxiang_purchase_ui import PurchaseDialog

    app = QApplication.instance() or QApplication([])
    app.setStyle('Fusion')
    palette = app.style().standardPalette()
    for role, color in [('Window', '#F3F6F7'), ('WindowText', '#22383C'), ('Base', '#FFFFFF'),
                        ('AlternateBase', '#F7FAFA'), ('Text', '#22383C'), ('Button', '#FFFFFF'),
                        ('ButtonText', '#22383C'), ('Highlight', '#DDEFEA'), ('HighlightedText', '#153C43')]:
        palette.setColor(getattr(QPalette.ColorRole, role), QColor(color))
    app.setPalette(palette)
    app.setFont(QFont('Microsoft YaHei', 10))
    app.setStyleSheet(STYLE)
    from test_aoxiang_purchase_ui import Client, record
    host = QWidget()
    host.work_repository = Repository(out / 'settings')
    windows = []
    images = []
    checks = []

    def wait(predicate):
        deadline = time.monotonic() + 6
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(20)
        assert predicate(), 'Qt operation timed out'

    def capture(widget, name):
        widget.show()
        app.processEvents()
        QTest.qWait(80)
        path = out / (name + '.png')
        assert widget.grab().save(str(path))
        images.append({'file': path.name, 'sha256': sha(path), 'source': 'source Qt; synthetic; no live platform'})

    c = schema.standard()
    base = spi.base()
    base['name'] = '演示门店 A'
    base['ui'].update(primary='山东仓', secondary=None, mode=1)
    form = PatchDialog(host, {'QA001': c})
    windows.append(form)
    form.search.setText('山东仓')
    check, mode, edit = form.controls[('warehouses', '山东仓', 0)]
    check.setChecked(True)
    edit.setText('7')
    capture(form, 'parameter-selected-fields')
    form.hide()

    detail = ParameterDialog(host, c, base['preset'], scope='所选演示门店 A · 本次作业')
    windows.append(detail)
    detail.resize(1360, 780)
    detail.tabs.setCurrentWidget(detail.weight_table)
    detail.weight_table.horizontalHeader().setStretchLastSection(True)
    for i, width in enumerate((200, 220, 230, 230)):
        detail.weight_table.setColumnWidth(i, width)
    detail.weight_table.setCurrentCell(1, 2)
    capture(detail, 'parameter-weights')
    detail.tabs.setCurrentWidget(detail.effective_now)
    capture(detail, 'parameter-effective-values')
    detail.hide()

    from openpyxl import load_workbook
    book = out / 'synthetic-weights.xlsx'
    parameter_tables.write(book, 'classified_parameters', {'QA001': base}, scoped=True)
    wb = load_workbook(book)
    ws = wb['分类参数']
    heads = {str(cell.value): cell.column for cell in ws[1]}
    row = next(i for i in range(2, ws.max_row + 1) if ws.cell(i, heads['分类组']).value == '日百')
    ws.cell(row, heads['最近7天权重'], .6)
    ws.cell(row, heads['此前7天权重'], .6)
    wb.save(book)
    wb.close()
    snapshot = {'saved': {'QA001': copy.deepcopy(base)}, 'current': {'QA001': copy.deepcopy(base)}, 'names': {'QA001': base['name']}}
    preview_host = SimpleNamespace(readonly=False, worker=None)
    bad = sui.Preview(host, preview_host, spi.read([str(book)]), snapshot, 'both')
    windows.append(bad)
    assert not bad.go.isEnabled() and bad.prepared['errors']
    capture(bad, 'parameter-import-invalid')
    checks.append({'case': 'w1=.6,w2=.6', 'errors': bad.prepared['errors'], 'apply_enabled': bad.go.isEnabled()})
    table = bad.findChild(QTableView)
    table.setCurrentIndex(table.model().index(0, 0))

    def capture_error_detail():
        dialog = QApplication.activeModalWidget()
        detail_table = dialog.findChild(QTableView)
        detail_table.setCurrentIndex(detail_table.model().index(2, 0))
        capture(dialog, 'parameter-import-error-detail')
        dialog.accept()

    from table_readability import full_row
    QTimer.singleShot(80, capture_error_detail)
    full_row(table)
    bad.hide()
    wb = load_workbook(book)
    wb['分类参数'].cell(row, heads['此前7天权重'], .4)
    wb.save(book)
    wb.close()
    good = sui.Preview(host, preview_host, spi.read([str(book)]), snapshot, 'both')
    windows.append(good)
    assert good.go.isEnabled() and not good.prepared['errors']
    capture(good, 'parameter-import-valid')
    checks.append({'case': 'w1=.6,w2=.4', 'errors': good.prepared['errors'], 'apply_enabled': good.go.isEnabled(), 'w3': 0})
    good.hide()

    # The client implements only local fixture responses. No browser subprocess,
    # component process, platform request or real purchase is started.
    purchase_host = SimpleNamespace(repository=SimpleNamespace(folder=out / 'settings'), work_repository=host.work_repository)
    head = {'number': 'BH-20261108-QA001-ZC-SD', 'name': '演示门店 A', 'warehouse': '山东仓', 'store': 'QA001'}
    purchase = PurchaseDialog(None, purchase_host, head, client_factory=Client)
    windows.append(purchase)
    purchase.resize(1360, 900)
    purchase.show()
    wait(lambda: purchase.configuration is not None and purchase.worker is None)
    purchase.client.records = [record(state='NEEDS_REVIEW', taskId=None,
        basis={'number': head['number'], 'store': head['store'], 'warehouse': head['warehouse']},
        target={'buyerWarehouseCode': 'QA001', 'displayName': head['name']})]
    purchase.load()
    wait(lambda: purchase.worker is None)
    purchase.current['totalAmount'] = '0.98'
    purchase.package = {'id': 'p1', 'intent': {'target': {'displayName': head['name']}, 'supplierCode': 'ZXC2025',
        'totalQuantity': '7', 'totalAmount': '0.98', 'rows': [{'sku': 'QA1001', 'name': '演示商品（非真实业务）', 'quantity': '7', 'unit_price': '0.14', 'amount': '0.98'}]}}
    purchase.basis_state = {'valid': True}
    purchase.basis_note.setText('演示资料：采购依据与已确认付款一致；未核验真实平台。')
    purchase.render()
    assert purchase.binding_start_btn.isEnabled()
    capture(purchase, 'purchase-binding-entry')
    errors = []

    def capture_request():
        try:
            modal = QApplication.activeModalWidget()
            assert isinstance(modal, QDialog)
            fields = modal.findChildren(QLineEdit)
            fields[0].setText('PO202611080000000001')
            fields[2].setText('演示：上传回执缺失，已在平台核实原单')
            capture(modal, 'purchase-binding-request')
            modal.reject()
        except Exception as exc:
            errors.append(repr(exc))
            if QApplication.activeModalWidget():
                QApplication.activeModalWidget().reject()

    QTimer.singleShot(80, capture_request)
    purchase.binding_start()
    assert not errors, errors
    # Show the READY state from synthetic evidence, without executing verification
    # or confirmation. It illustrates the existing form, not a live acceptance.
    proof = out / 'synthetic-proof.json'
    proof.write_text(json.dumps({'evidence': {'observedAt': '2026-11-08T10:00:00+08:00',
        'rows': [{'sku': 'QA1001', 'quantity': '7', 'amount': '0.98', 'status': '待提交'}]}}, ensure_ascii=False), 'utf-8')
    purchase.binding_status({'purchase_id': 'p1', 'sequence': 5, 'active': False, 'proposal': {
        'id': 'manual-demo', 'state': 'READY', 'request': {'order_id': 'PO202611080000000001', 'created_day': '', 'reason': '演示核对'},
        'summary': {'status': '待提交', 'rows': 1, 'quantity': '7', 'amount': '0.98'},
        'manifest': {'file': str(proof), 'sha256': sha(proof)}}})
    assert purchase.binding_confirm_btn.isEnabled()
    capture(purchase, 'purchase-binding-ready')

    def capture_rows():
        modal = QApplication.activeModalWidget()
        if modal is None:
            QTimer.singleShot(60, capture_rows)
            return
        capture(modal, 'purchase-binding-rows')
        modal.accept()

    QTimer.singleShot(100, capture_rows)
    purchase.binding_details()
    assert not any(action in ('purchase.start', 'binding.start', 'binding.confirm') for action, _ in purchase.client.calls)
    checks.append({'case': 'AX existing binding forms', 'client_actions': [a for a, _ in purchase.client.calls], 'live_platform': False, 'binding_confirm_clicked': False})
    purchase.close()
    wait(lambda: purchase._can_close and purchase.worker is None)
    for widget in windows[:-1]:
        widget.close()
    host.close()
    metadata = {'synthetic': True, 'live_platform_tested': False, 'images': images, 'checks': checks,
        'standard': c, 'definitions': guidance.definitions()}
    (out / 'capture-report.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps({'images': len(images), 'checks': len(checks), 'live_platform': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()
