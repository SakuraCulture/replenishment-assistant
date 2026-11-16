"""Opt-in real-window acceptance on newly generated data; transport stays offline."""
import copy
import json
import os
import shutil
import sqlite3
import time
import traceback
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QProcess, QProcessEnvironment, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox, QPushButton


def start_smoke(app, win, args):
    out, phase = Path(args[0]).resolve(), args[1]
    fixture = json.loads((out / 'fixture.json').read_text(encoding='utf-8'))
    assert fixture['synthetic'] and not fixture['live_platform']
    path = Path(fixture['path']).resolve()
    assert path == win.work_repository.path.resolve() and path.is_relative_to(out)
    win.auto_check.stop();win.jst_generator.stopped = True;win.jst_generator.timer.stop()
    manager = win.aoxiang_workflows;manager.timer.stop();manager.stopping = True
    checks = [];dialogs = [];unexpected = [];children = []
    import order_lifecycle as lc, purchases, warehouse_execution as execution, normal_orders as normal, work_repository as wr
    from aoxiang_directory import atomic_json
    from aoxiang_purchase_tasks import prepare, validate_for_upload
    from aoxiang_ui_common import component_root
    from aoxiang_receipt_scope import assess
    from aoxiang_workflows import Workflows
    from version_info import diagnostic
    component = component_root()

    def require(ok, text):
        if not ok:raise AssertionError(text)
        checks.append(text)

    def wait(test, seconds=70):
        until = time.monotonic() + seconds
        while not test() and time.monotonic() < until:QTest.qWait(20)
        require(bool(test()), '后台动作已安全完成')

    def modal():
        d = app.activeModalWidget()
        if d is None:return
        if isinstance(d, QMessageBox):
            if d.windowTitle() == '启用周期同步':
                QTest.mouseClick(d.button(QMessageBox.StandardButton.Yes), Qt.MouseButton.LeftButton)
            else:
                unexpected.append(d.windowTitle() + ': ' + d.text());d.reject()
        elif d.windowTitle() in ('导出订单级对比', '对比报表已导出'):
            text = '选择保存位置并导出' if d.windowTitle().startswith('导出') else '关闭'
            button = next((b for b in d.findChildren(QPushButton) if text in (b.text() or b.accessibleName())), None)
            if button:QTest.mouseClick(button, Qt.MouseButton.LeftButton)

    timer = QTimer(win);timer.setInterval(20);timer.timeout.connect(modal);timer.start()

    def race(mode):
        case = out / ('race-' + mode);settings = case / 'settings';settings.mkdir(parents=True)
        case_path = settings / 'workbench.sqlite3'
        with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(case_path)) as target:source.backup(target)
        shutil.copytree(path.parent / 'warehouse_catalogs', settings / 'warehouse_catalogs')
        state = settings / 'aoxiang/component';shutil.copytree(Path(fixture['component_template']), state)
        package = prepare(case_path, fixture['head'], fixture['config'], fixture['suppliers'], fixture['receipt'], case / 'packages', component / 'assets/purchase-import-template-contract.json')
        validate_for_upload(case_path, package, fixture['config'], fixture['suppliers'])
        info = {'synthetic': True, 'fixture_root': str(case), 'business_path': str(case_path), 'component': str(state), 'package': package,
                'barrier': {'ready': str(case / 'ready.json'), 'go': str(case / 'continue.json')}}
        input_file = case / 'input.json';atomic_json(input_file, info)
        process = QProcess(win);process.setProcessEnvironment(QProcessEnvironment.systemEnvironment());children.append((process, case))
        process.start(str(component / 'runtime/node.exe'), [str(out / 'offline-transport.mjs'), str(input_file), mode, str(component)])
        wait(lambda: (case / 'ready.json').is_file() or process.state() == QProcess.ProcessState.NotRunning)
        if not (case / 'ready.json').is_file():
            (case / 'component.log').write_text(bytes(process.readAllStandardOutput()).decode('utf-8')+'\n'+bytes(process.readAllStandardError()).decode('utf-8'),encoding='utf-8')
        require((case / 'ready.json').is_file(), '实际组件到达受控同步点：' + mode)
        receipt = lc.commit(case_path, fixture['new_payment_plan'])
        require(bool(receipt), '实际EXE保存付款7改2，未被浏览器等待或提交登记锁住：' + mode)
        atomic_json(case / 'continue.json', {'continue': True})
        wait(lambda: process.state() == QProcess.ProcessState.NotRunning)
        stdout = bytes(process.readAllStandardOutput()).decode('utf-8');stderr = bytes(process.readAllStandardError()).decode('utf-8')
        (case / 'component.log').write_text(stdout + '\n' + stderr, encoding='utf-8')
        require(process.exitCode() == 0, '实际随包组件正常退出：' + mode)
        result = json.loads(stdout.splitlines()[-1]);atomic_json(case / 'result.json', result)
        if mode == 'after_boundary':
            require(result['released'] == 1 and result['state'] == 'ACCEPTED' and result['quantity'] == '7', '提交后变更保留原冻结数量及唯一受理记录')
        else:
            require(result['released'] == 0 and not result['uploadAttempted'] and result['error']['code'] == 'PURCHASE_BASIS_CHANGED', '最终核验拒绝旧数量，明确未上传：' + mode)
        require(lc.snapshot(case_path, fixture['head'])['documents']['payment']['payload']['rows'][0]['base_quantity'] == '2', '当前付款事实仍为2：' + mode)

    def exercise():
        for mode in ('prepare', 'connect', 'request', 'after_boundary'):race(mode)
        with closing(win.work_repository.connect()) as db, db:execution.ensure_parent(db, fixture['head']['oid'])
        h = next(h for h in purchases.targets(path) if h['warehouse'] == '河北仓')
        payment = lc.snapshot(path, h)['documents']['payment']
        basis = {k: h[k] for k in ('oid', 'revision', 'warehouse', 'store', 'number')}
        basis.update(signedDigest=h['digest'], visibilityToken=h.get('visibility_token', 0), paymentVersion=payment['version'], paymentDigest=payment['digest'])
        record = {'id': 'synthetic-receipt', 'state': 'BOUND', 'basis': basis, 'target': {'displayName': '隔离验收门店'}, 'binding': {'orderId': 'OFFLINE-PO-HB', 'status': '待提交'}}
        store = Workflows(path.parent / 'aoxiang', business_path=path)
        store.configure_schedules([record], 5, True)
        with store.db(True) as db:
            s = store.schedule(record['id']);s.update(retired=True, retired_reason='原签发修订已失效', enabled=False, automation_paused=True)
            db.execute('UPDATE ax_schedules SET payload=? WHERE id=?', (json.dumps(s), record['id']))
        payload = copy.deepcopy(fixture['payload']);payload['result']['rows'][1]['D'] += ' 山东独立更正'
        digest = normal.fingerprint_owned(payload['result'], payload.get('new_focus'))
        with closing(win.work_repository.connect()) as db, db:
            db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)', (h['oid'], 2, digest, wr.now(), normal.encode(payload, db)))
            db.execute('UPDATE normal_orders SET current=2 WHERE oid=?', (h['oid'],))
            execution.publish(db, h['oid'], 2, digest, wr.now(), payload, ['山东仓'])
        require(assess(path, [record])[record['id']]['eligible'], '山东重签后河北原采购仍有效')
        class LocalClient:
            def __init__(self, *args):self.running = False
            def start(self):self.running = True
            def close(self, **kwargs):self.running = False;return True
            def request(self, action, **kwargs):
                if action == 'configuration':return fixture['config']
                if action == 'purchase.list':return {'total': 1, 'items': [record]}
                if action == 'receipt.list':return {'total': 0, 'items': []}
                raise AssertionError('No platform transport allowed: ' + action)
        host = SimpleNamespace(repository=win.repository, work_repository=win.work_repository,
                               aoxiang_workflows=SimpleNamespace(gate=manager.gate, client_factory=manager.client_factory, store=store, ensure_owner=lambda: None))
        from aoxiang_workflow_ui import ReceiptSelection
        dialog = ReceiptSelection(win, host, [h], client_factory=LocalClient);dialogs.append(dialog);dialog.show()
        wait(lambda: dialog.worker is None and len(dialog.records) == 1)
        require(dialog.records[0]['eligible'], '实际批量收货窗口显示有效河北仓单')
        dialog.table.item(0, 0).setCheckState(Qt.CheckState.Checked);dialog.enable_btn.click()
        saved = store.schedule(record['id'])
        require(saved['enabled'] and not saved.get('retired') and saved['order_id'] == 'OFFLINE-PO-HB', '实际启用按钮重核验并恢复误退役监听，保留原采购单号')
        dialog.grab().save(str(out / '实际恢复误退役监听.png'))
        dialog.table.item(0, 0).setCheckState(Qt.CheckState.Checked);dialog.now_btn.click()
        wait(lambda: dialog.worker is None and dialog._close_done)
        require(bool(dialog.created_job) and store.get(dialog.created_job['id'])['items'][0]['order_id'] == 'OFFLINE-PO-HB', '实际立即同步按钮继续原采购，不创建新采购单')
        from lifecycle_home_ui import LifecycleWidget
        widget = LifecycleWidget(win);dialogs.append(widget);widget.resize(1240, 800);widget.show();widget.query()
        wait(lambda: len(widget.records) == 2)
        index = next(i for i, r in enumerate(widget.records) if r.get('warehouse') == '河北仓')
        widget.table.item(index, 0).setCheckState(Qt.CheckState.Checked)
        require(len(widget.checked) == 1, '实际订单列表只勾选有效河北仓单')
        target = out / '实际河北生命周期对比.xlsx'
        with patch('export_locations.save_file', return_value=(str(target), 'Excel (*.xlsx)')):widget.order_compare_btn.click()
        require(target.is_file(), '实际导出按钮完成父单修订不同时的河北对比导出')
        import openpyxl
        book = openpyxl.load_workbook(target, read_only=True, data_only=True)
        try:
            rows=list(book['订单级对比'].iter_rows(values_only=True))
            require(len(rows)==2 and rows[1][4]==h['number'], '导出文件可打开，仓单范围与原单号一致')
        finally:book.close()
        widget.grab().save(str(out / '实际分仓对比导出.png'));widget.close()
        atomic_json(out / 'reopen.json', {'head': h, 'purchase_id': record['id'], 'job_id': dialog.created_job['id']})

    def reopen():
        state = json.loads((out / 'reopen.json').read_text(encoding='utf-8'));h = state['head']
        store = Workflows(path.parent / 'aoxiang', business_path=path);s = store.schedule(state['purchase_id'])
        require(s['enabled'] and not s.get('retired') and s['order_id'] == 'OFFLINE-PO-HB', '实际退出重开后恢复的监听与原采购身份保留')
        require(lc.snapshot(path, h)['health']['payment'], '重开后河北付款依据仍健康，数量与版本保持')
        with closing(win.work_repository.connect()) as db:purchases.validate_head(db, h)
        require(h['revision'] == 1, '父单修订2时河北有效源修订仍为1')
        require(store.get(state['job_id'])['items'][0]['order_id'] == 'OFFLINE-PO-HB', '重开后继续任务仍指向原采购单')
        win.grab().save(str(out / '实际退出重开.png'))

    def run():
        try:
            (exercise if phase == 'exercise' else reopen)()
            require(not unexpected, '没有忽略真实错误弹窗：' + str(unexpected))
            require(not win.browser_startup.started, '未启动真实平台连接')
            result = {'passed': True, 'phase': phase, 'checks': checks, 'live_platform': False, **diagnostic()}
        except Exception:
            result = {'passed': False, 'phase': phase, 'checks': checks, 'error': traceback.format_exc(), 'live_platform': False}
        finally:
            for process, case in children:
                if process.state() != QProcess.ProcessState.NotRunning:
                    atomic_json(case / 'continue.json', {'cancel': True})
                    until = time.monotonic() + 15
                    while process.state() != QProcess.ProcessState.NotRunning and time.monotonic() < until:QTest.qWait(20)
        atomic_json(out / ('report-' + phase + '.json'), result)
        for d in dialogs:
            try:d.close()
            except RuntimeError:pass
        if result['passed']:win.close()
        else:app.exit(1)
    QTimer.singleShot(350, run)
