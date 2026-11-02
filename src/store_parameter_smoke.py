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
            ws.append(['预算金额', '门店编码', '山东仓备货天数', '库存目标分位概率','分类参数/快销/常规资格周转阈值','分类参数/快销/低月销基本售价门槛','分类参数/日百/常规资格周转阈值','分类参数/日百/低月销基本售价门槛','分类参数/快销/库存目标分位概率','分类参数/日百/库存目标分位概率'])
            ws.append([0, a, 10, .8,7,10,12,6,.95,.9])
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
            overrides={'快销':{'moq_days':7,'moq_price':10,'probability':.95},'日百':{'moq_days':12,'moq_price':6,'probability':.9}}
            if win.config_for(a).get('profile_parameters')!=overrides:raise AssertionError('Category overrides did not reload')
            from parameter_ui import ParameterDialog
            editor=ParameterDialog(win,win.config_for(a),win.preset_for(a),scope='所选门店')
            editor.show();app.processEvents()
            if editor.value()!=win.config_for(a):raise AssertionError('Opening editor altered category configuration')
            editor.tabs.setCurrentWidget(editor.profile_table);app.processEvents();editor.grab().save(str(out/'03_分类超参.png'));editor.reject()
            spi.write(out/'完整参数回导.xlsx',{a:saved[a]})
            roundtrip=spi.plan(spi.read([out/'完整参数回导.xlsx']),{a:saved[a]})
            if roundtrip['errors'] or roundtrip['changed']:raise AssertionError('Full category export roundtrip changed configuration')
            checks.append('category thresholds: saved defaults, editor roundtrip, Chinese form and full Excel reimport')
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
            manual['parameter_config']['profile_parameters']=overrides
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
            # Reproduce the real checked-file callback, not populate_stores alone.
            win.reset_job();win.catalogs=catalogs;win.profile=profile
            win.loaded_ok(copy.deepcopy(loaded))
            updated=copy.deepcopy(win.work_repository.stores()[a])
            updated['preset']['config']['warehouses']['山东仓']=[11,3.5]
            updated['preset']['config']['profile_parameters']['快销']['probability']=.93
            updated['ui'].update(mode=1,amount='',primary='河北仓',secondary='山东仓')
            win.work_repository.save_stores({a:updated})
            win.sales.paths=[str(fixture/'sales.xlsx')];win.sales.refresh();win.start_load()
            import time
            deadline=time.monotonic()+30
            while win.worker is not None and time.monotonic()<deadline:
                app.processEvents();QTest.qWait(20)
            if win.worker is not None:raise AssertionError('Checked-file reload did not finish')
            if win.config_for(a)!=updated['preset']['config']:raise AssertionError('Recheck replaced new defaults with old UI values')
            actual=win.selected_settings(a)
            if actual['supply_route']!={'primary':'河北仓','secondary':'山东仓'} or actual['budget'] is not None:
                raise AssertionError('Reload did not refresh inherited route/budget')
            got=batch_service.calculate_store(win.loaded,a,actual,profile)
            manual={**actual,'parameter_config':copy.deepcopy(updated['preset']['config']),
                    'warehouses':copy.deepcopy(updated['preset']['config']['warehouses'])}
            expected=batch_service.calculate_store(win.loaded,a,manual,profile)
            if got['rows']!=expected['rows'] or got['summary']!=expected['summary']:
                raise AssertionError('Reloaded defaults did not reach per-SKU calculation')
            win.navigate(1);win.show_section(1);app.processEvents();win.grab().save(str(out/'07_重新检查带入最新默认.png'))
            checks.append('actual background check-file reload adopts changed durable defaults; effective settings, per-SKU rows and summary equal explicit expected configuration')
            own=win.config_for(a);own['parameters']['cap_days']=18
            win.apply_parameters([a],own,win.preset_for(a))
            win.set_store_ui(a,{**win.store_ui(a),'mode':2,'amount':'0'})
            win.loaded_ok(copy.deepcopy(loaded))
            if win.config_for(a)!=own or win.selected_settings(a)['budget']!=0:
                raise AssertionError('Recheck discarded explicit current-job parameters/budget')
            checks.append('explicit current-job parameter and zero-budget edits survive recheck')
            if len(args)>2:
                plan=json.loads(Path(args[2]).read_text(encoding='utf-8'));records=plan['records']
                if not plan['complete']:raise ValueError('Real parameter plan is incomplete')
                old=win.work_repository.stores();win.work_repository.save_stores(records,expected={code:old.get(code) for code in records})
                readback=win.work_repository.stores()
                if any(readback.get(code)!=value for code,value in records.items()):raise AssertionError('88-store EXE readback mismatch')
                win.parameter_scene.refresh();win.show_section(4);app.processEvents();win.grab().save(str(out/'04_88店参数.png'))
                code=next(iter(records));rec=readback[code]
                editor=ParameterDialog(win,rec['preset']['config'],rec['preset'],scope='所选门店 '+rec['name'],readonly=True)
                editor.show();editor.tabs.setCurrentWidget(editor.profile_table);app.processEvents()
                if editor.value()!=rec['preset']['config']:raise AssertionError('Actual parameters altered by form')
                editor.grab().save(str(out/'05_实际参数分类表单.png'));editor.tabs.setCurrentIndex(editor.tabs.count()-1);app.processEvents();editor.grab().save(str(out/'06_原表依据表单.png'));editor.reject()
                checks.append(f'isolated application: {len(records)} real store configurations saved and reloaded exactly; category/source forms')
                # Exercise every injected code through the UI -> worker-settings boundary.
                # These are synthetic rows, not additional real sales records.
                sample=copy.deepcopy(loaded)
                seed=next((d,m) for d,m in sample['normalized'] if d['B']==a)
                sample['stores']={code:value.get('name',code) for code,value in records.items()}
                sample['normalized']=[];sample['blocked']={}
                for code,name in sample['stores'].items():
                    d,m=copy.deepcopy(seed);d.update(A=name,B=code);sample['normalized'].append((d,m))
                win.reset_job();win.catalogs=catalogs;win.loaded_ok(sample)
                for code,value in records.items():
                    if win.store_ui(code)['mode']!=value['ui'].get('mode',0):raise AssertionError('Saved budget mode changed: '+code)
                    # Source sheet intentionally has no budget for new stores.
                    if win.store_ui(code)['mode']==0:win.set_store_ui(code,{**win.store_ui(code),'mode':1})
                    settings=win.selected_settings(code)
                    if settings['parameter_config']!=value['preset']['config'] or settings['warehouses']!=value['preset']['config']['warehouses']:
                        raise AssertionError('Injected store defaults not passed to task: '+code)
                    route={k:value['ui'].get(k) for k in ('primary','secondary')}
                    if settings['supply_route']!=route:raise AssertionError('Injected warehouse route differs: '+code)
                checks.append(f'{len(records)} injected codes: fresh import callback, all config values, warehouse cycles and route match worker settings; synthetic row shells')
            result = {'passed': True, 'frozen_exe': bool(getattr(sys, 'frozen', False)), 'checks': checks, **diagnostic()}
        except Exception:
            result = {'passed': False, 'checks': checks, 'error': traceback.format_exc(), **diagnostic()}
        (out / 'exe-report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        app.exit(0 if result['passed'] else 1)
    QTimer.singleShot(200, execute)
