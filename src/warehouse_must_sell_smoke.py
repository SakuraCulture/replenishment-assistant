"""Exercise the frozen application with matched, verifier-created warehouse samples."""
import copy
import json
import os
import sys
import time
import traceback
from contextlib import closing
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QComboBox, QPushButton, QTableWidget


def start_smoke(app, win, args):
    out = Path(args[0]).resolve()
    phase = args[1]
    spec = json.loads((out / 'synthetic.json').read_text(encoding='utf-8'))
    if (phase not in ('calculate', 'reopen') or spec.get('synthetic') is not True
            or spec.get('live_platform') is not False or spec.get('case') != 'warehouse-must-sell'
            or Path(os.environ.get('REPLENISHMENT_DATA_DIR', '')).resolve() != out / 'settings'
            or win.work_repository.path.resolve() != out / 'settings/workbench.sqlite3'):
        raise ValueError('需要独立的按仓必卖验收资料目录')

    from business_presenter import title
    from history_ui import ArchiveDialog
    from order_analytics import membership
    from result_semantics import order_quantity
    from ui_work_io import Session, request
    from version_info import diagnostic
    from warehouse_catalog import revision
    from warehouse_execution import get_order, heads

    errors = []
    win.failed = errors.append
    win.jst_generator.stopped = True
    win.jst_generator.timer.stop()
    win.aoxiang_workflows.stopping = True
    win.aoxiang_workflows.timer.stop()
    win.browser_status.paused = True
    win.browser_status.timer.stop()
    win.browser_status.initial.stop()
    win.scheduled.timer.stop()
    skus = spec['skus']
    legacy_store = spec['legacy_archive']['store']
    sd = '山东仓'

    def wait(predicate, label):
        deadline = time.monotonic() + 40
        while not errors and not predicate() and time.monotonic() < deadline:
            QTest.qWait(25)
        assert not errors and predicate(), (label, errors, win.status.text())

    def shot(widget, name):
        assert widget.grab().save(str(out / f'{phase}-{name}.png'))

    def history(store, expected, original):
        with closing(win.work_repository.connect()) as db:
            head, = [h for h in heads(db) if h['store'] == store]
        order = get_order(win.orders, head)
        result = order['payload']['result']
        snapshot = {key: head[key] for key in ('oid', 'number', 'warehouse', 'revision', 'digest', 'snapshot_digest')}
        snapshot.update(store=store, list_revision=result['must_list']['revision'])
        assert all(snapshot[key] == value for key, value in original.items()), snapshot
        displayed = []
        dialog = ArchiveDialog(win, head, initial_tab='audit', sku=skus[0])
        try:
            dialog.show()
            wait(lambda: dialog.selected and 2 in dialog.children_views, '打开历史签发')
            assert [r[0] for r in dialog.model.rows] == skus
            audit = dialog.children_views[2]
            scope = '商品完整计算依据'
            audit.scope.setCurrentText(scope)
            for index, (sku, row) in enumerate(zip(skus, result['rows'], strict=True)):
                assert row['E'] == sku and row['AU'] == sd and row['_must_sell'] is expected
                assert membership(result, row, sku, sd) == ('必采' if expected else '非必采')
                audit.sku.setText(sku)
                audit.load()
                wait(lambda: audit.loaded_key == (scope, sku), '读取历史商品依据')
                field, = [r for r in audit.model.rows if r[0] == title('_must_sell')]
                value = '是' if expected else '否'
                assert field[1] == value, field
                audit.search.setText(title('_must_sell'))
                shot(dialog, f'{store}-{index+1}-历史签发')
                displayed.append({'sku': sku, 'frozen_mandatory': expected, 'ui_value': value,
                                  'quantity': order_quantity(row)})
        finally:
            dialog.reject()
            dialog.deleteLater()
        return {**snapshot, 'rows': displayed}

    def read_list(expected, label):
        inspected = []

        def check_dialog():
            dialog = app.activeModalWidget()
            try:
                table = dialog.findChild(QTableWidget)
                selector = dialog.findChild(QComboBox)
                assert table.horizontalHeaderItem(0).text() == '供货仓'
                for warehouse, codes in expected.items():
                    selector.setCurrentText(warehouse)
                    actual = [table.item(row, 1).text() for row in range(table.rowCount()) if not table.isRowHidden(row)]
                    assert actual == codes, (warehouse, actual, codes)
                    shot(dialog, f'{label}-{warehouse}-当前清单')
                inspected.append(True)
            except BaseException:
                errors.append(traceback.format_exc())
            finally:
                if dialog is not None:
                    dialog.accept()

        win.show_section(1)
        win.navigate(0)
        buttons = win.import_page.findChildren(QPushButton)
        assert not any('导入' in b.text() and '必卖清单' in b.text() for b in buttons)
        button, = [b for b in buttons if b.text() == '查看各仓清单']
        assert button.isEnabled()
        QTimer.singleShot(0, check_dialog)
        button.click()
        assert inspected == [True], errors

    def run():
        report = {'phase': phase, 'passed': False, 'frozen_exe': bool(getattr(sys, 'frozen', False)),
                  'build': diagnostic(), 'live_platform': False, 'source_catalogs': spec['source_catalogs']}
        try:
            if phase == 'calculate':
                read_list(spec['initial_list'], '原始标签')
                win.sales.add_paths([str(out / 'sales.xlsx')])
                win.auto_check.stop()
                win.start_load()
                wait(lambda: win.worker is None and win.loaded is not None and not win.input_dirty, '检查验收文件')
                assert list(win.loaded['stores']) == list(spec['routes'])
                assert win.profile['schema'] == 2
                for store, case in spec['routes'].items():
                    win.set_store_ui(store, {'mode': 1, 'primary': case['primary'], 'secondary': case['secondary']})
                win.start_calculation()
                wait(lambda: win.worker is None and set(win.results) == set(spec['routes']), '完成真实门店计算')
                cases = []
                for store, result in win.results.items():
                    case = spec['routes'][store]
                    warehouse = case['actual']
                    assert [r['E'] for r in result['rows']] == skus
                    catalog = win.catalogs[warehouse]
                    for row in result['rows']:
                        sku = row['E']
                        expected = spec['initial_rows'][warehouse][sku]
                        assert row['AU'] == warehouse and row['_must_sell'] is expected['must_sell']
                        assert row['S'] == expected['status'] and row['_meta']['product_revision'] == catalog['revision']
                        qty = order_quantity(row)
                        assert qty == (0 if row['S'] == '禁用' else expected['quantity'])
                        cases.append({'store': store, 'sku': sku, 'primary': case['primary'], 'secondary': case['secondary'],
                                      'actual': row['AU'], 'must_sell': row['_must_sell'], 'status': row['S'],
                                      'quantity': qty, 'catalog_revision': catalog['revision'], 'worker_pid': result['task']['pid']})
                report['cases'] = cases
                report['legacy_archive'] = history(legacy_store, True, spec['legacy_archive'])

                sd_store, = [store for store, case in spec['routes'].items() if case['actual'] == sd]
                with Session(win.work_repository.path) as session:
                    request(win, session, 'prepare_normal', [win.snapshot(), [sd_store]])
                    receipt = request(win, session, 'confirm_normal', None)
                report['signed'] = receipt['orders']
                summary = win.results[sd_store]['must_list']
                report['tag_archive'] = history(sd_store, False, {'list_revision': summary['revision']})

                updated = copy.deepcopy(win.catalogs[sd])
                updated['items'][skus[0]]['商品标签'] = '必卖清单'
                updated['revision'] = revision(updated['items'])
                win.catalog_repository.save(updated)
                win.current_warehouse_adopted(updated)
                assert win.input_dirty
                assert win.profile['warehouses'][sd]['catalog_revision'] == updated['revision']
                report['changed_catalog_revision'] = updated['revision']
                read_list({**spec['initial_list'], sd: [skus[0]]}, '资料刷新后')
            else:
                previous = json.loads((out / 'calculate-report.json').read_text(encoding='utf-8'))
                assert previous['passed']
                read_list({**spec['initial_list'], sd: [skus[0]]}, '重开后')
                assert win.profile['warehouses'][sd]['catalog_revision'] == previous['changed_catalog_revision']
                for key, expected in (('legacy_archive', True), ('tag_archive', False)):
                    archive = previous[key]
                    report[key] = history(archive['store'], expected, {k: v for k, v in archive.items() if k != 'rows'})
                    assert report[key] == archive
            report['passed'] = True
        except BaseException:
            report['error'] = traceback.format_exc()
        with (out / f'{phase}-report.json').open('x', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
        win.job_dirty = False
        win._discard_close = True
        if report['passed']:
            win.close()
        else:
            app.exit(1)

    QTimer.singleShot(0, run)
