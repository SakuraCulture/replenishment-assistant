"""Opt-in actual application smoke; launcher must supply a separate QA data directory."""
import copy
import json
import os
import sys
import traceback
from pathlib import Path
from PySide6.QtCore import QTimer, Qt, QMimeData, QUrl, QPointF, QPoint
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit
import openpyxl
import batch_service
import normal_exports
import lifecycle_io
import parameter_schema
import store_parameter_import as spi
import store_parameter_ui as ui
import work_repository
import warehouse_catalog
from import_ui import ImportDialog
from business_ui import run
from version_info import diagnostic


def start_smoke(app, win, args):
    fixture, out = map(lambda p: Path(p).resolve(), args[:2])
    def execute():
        checks = []
        try:
            if Path(os.environ.get('REPLENISHMENT_DATA_DIR', '')).resolve() != out / 'settings':
                raise ValueError('Smoke requires its own isolated settings directory')
            out.mkdir(parents=True, exist_ok=True)
            # Import via the normal checked-file service, and create real store widgets.
            catalogs = {wh: warehouse_catalog.import_catalog(fixture / (wh + '.xlsx'), wh) for wh in warehouse_catalog.WAREHOUSES}
            loaded = batch_service.load([fixture / 'sales.xlsx'], catalogs)
            profile = json.loads((fixture / 'must.json').read_text(encoding='utf-8'))
            win.loaded_ok(loaded)
            codes = list(win.store_widgets)
            if len(codes) < 2:
                raise ValueError('Fixture requires two stores')
            a, b = codes[:2]
            for code in codes:
                win.set_store_ui(code, {**win.store_ui(code), 'mode': 1, 'primary': '河北仓', 'secondary': '山东仓'})
            old_b = copy.deepcopy(win.selected_settings(b))
            settings = win.selected_settings(a)
            original = batch_service.calculate_store(loaded, a, settings, profile)
            win.results[a] = copy.deepcopy(original)
            win.states[a]['state'] = '已完成'
            win.results[b] = batch_service.calculate_store(loaded, b, win.selected_settings(b), profile)
            win.states[b]['state'] = '已完成'
            file = out / '门店参数试验.xlsx'
            book = openpyxl.Workbook()
            ws = book.active
            ws.append(['预算金额', '门店编码', '山东仓备货天数', '库存目标分位概率'])
            ws.append([0, a, 10, .8])
            book.save(file)
            book.close()
            picker = ImportDialog(win, 'store_parameters', multiple=True)
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(file))])
            enter = QDragEnterEvent(QPoint(10, 10), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            picker.dragEnterEvent(enter)
            drop = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            picker.dropEvent(drop)
            if not enter.isAccepted() or picker.paths != [str(file)]:
                raise AssertionError('Excel drop did not select file')
            spi.write(out / '门店参数最低模板.xlsx')
            data = run(win, spi.read, (picker.paths,), '验收：后台读取门店参数')
            snapshot = ui.capture(win, data)
            before_defaults = win.work_repository.stores()
            d = ui.Preview(win, win, data, snapshot, 'current')
            d.show()
            app.processEvents()
            if not d.go.isEnabled():
                raise AssertionError(d.error.text())
            d.grab().save(str(out / '01_逐店差异预览.png'))
            QTest.keyClick(d, Qt.Key.Key_Escape)
            if d.result() != QDialog.DialogCode.Rejected or win.work_repository.stores() != before_defaults:
                raise AssertionError('Preview cancel changed defaults')
            checks.append('real Qt drag/drop, background read, template, preview and Esc cancel')
            d = ui.Preview(win, win, data, snapshot, 'both')
            d.show()
            app.processEvents()
            QTest.mouseClick(d.go, Qt.MouseButton.LeftButton)
            if d.result() != QDialog.DialogCode.Accepted:
                raise AssertionError('Explicit preview confirmation failed')
            ui.verify_sources(data)
            ui.commit(win, data, snapshot, 'both', d.prepared,
                persist=lambda values, expected: run(win, ui.save_values,
                    (win.work_repository, values, expected), '验收：原子保存参数', cancellable=False))
            if win.states[a]['state'] != '待重算' or win.states[b]['state'] != '已完成':
                raise AssertionError('Invalidation affected wrong store')
            if win.selected_settings(b) != old_b or win.results[a] != original:
                raise AssertionError('Other store or original result changed')
            saved = work_repository.Repository(win.repository.folder).stores()
            if saved[a]['ui']['amount'] != '0':
                raise AssertionError('Persisted budget did not reload')
            win.populate_stores()
            if win.config_for(a)['warehouses']['山东仓'][0] != 10 or win.store_ui(a)['amount'] != '0':
                raise AssertionError('Next populate did not load saved defaults')
            win.show_section(4)
            win.parameter_scene.refresh()
            win.grab().save(str(out / '02_参数设置.png'))
            checks.append('confirm both; atomic save; next store population reloads defaults; only affected result expires')
            imported = batch_service.calculate_store(loaded, a, win.selected_settings(a), profile)
            manual = copy.deepcopy(settings)
            manual['budget'] = 0
            manual['parameter_config']['warehouses']['山东仓'] = [10, manual['parameter_config']['warehouses']['山东仓'][1]]
            manual['warehouses'] = copy.deepcopy(manual['parameter_config']['warehouses'])
            manual['parameter_config']['parameters']['probability'] = .8
            expected = batch_service.calculate_store(loaded, a, manual, profile)
            if imported['rows'] != expected['rows'] or imported['summary'] != expected['summary']:
                raise AssertionError('Imported and manually configured calculation differ')
            checks.append('imported effective configuration matches manual calculation rows and summary exactly')
            # A signed copy retains its linkage columns and can be scanned as feedback.
            order = {'number': 'BH-QA-ZC', 'revision': 1, 'current_revision': 1, 'day': work_repository.today(),
                     'payload': {'result': original, 'new_focus': None}}
            signed = out / '签发回传验收.xlsx'
            normal_exports.export(order, signed)
            book = openpyxl.load_workbook(signed)
            for ws in book:
                if not ws.column_dimensions['U'].hidden or ws['U1'].value != '补货单号' or ws['V1'].value != '决策理由':
                    raise AssertionError('Signed number visibility/column layout incorrect')
            book.close()
            info = lifecycle_io.inspect([signed])[0]
            specs = []
            for sheet in info['sheets']:
                if '商品编码' not in sheet['headers']:
                    continue
                mapping = {f: sheet['headers'].index(f) if f in sheet['headers'] else None for f in lifecycle_io.fields('return')}
                mapping['门店确认数量'] = sheet['headers'].index('补货数量')
                mapping['供货仓'] = sheet['headers'].index('所属仓')
                specs.append({**info, **sheet, 'mapping': mapping, 'defaults': {'数量单位': '库存单位'}})
            scan = lifecycle_io.scan('return', specs)
            if not scan['rows'] or any(r['signed_number'] != order['number'] for r in scan['rows']):
                raise AssertionError('Hidden number not retained for feedback linkage')
            checks.append('running application export: all three signed sheets hide U, preserve values and feedback order linkage')
            result = {'passed': True, 'frozen_exe': bool(getattr(sys, 'frozen', False)), 'checks': checks, **diagnostic()}
        except Exception:
            result = {'passed': False, 'checks': checks, 'error': traceback.format_exc(), **diagnostic()}
        (out / 'exe-report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        app.exit(0 if result['passed'] else 1)
    QTimer.singleShot(200, execute)
