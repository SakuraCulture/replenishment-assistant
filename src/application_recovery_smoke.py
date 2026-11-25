"""Offline desktop acceptance of the actual watchdog and scheduled restart path."""
import os
import shutil
import sys
import traceback
from pathlib import Path

from stockout_common import atomic_json, read_json


class RecordedCollection:
    def __init__(self, out, manifest):
        self.folder = out / 'controlled-aoxiang'
        self.path = out / 'platform-command.json'
        self.trace = out / 'platform-actions.json'
        self.sources = manifest['files']
        self.account = {'account_id': 'isolated', 'merchant_code': 'isolated'}

    def request(self, action, **values):
        from aoxiang_jobs import fingerprint
        events = read_json(self.trace) if self.trace.exists() else []
        atomic_json(self.trace, events + [action])
        if action == 'configuration':
            return {'configured': True, 'enabled': True, 'account_binding': self.account, 'store_mapping_version': '0'*64,
                    'targets': [{'buyerWarehouseCode': f['warehouse_code'], 'mappingVerifiedAt': 'fixture'} for f in self.sources]}
        if action == 'reserve':
            assert not self.path.exists(), 'A prior reserve must be read back instead of submitted'
            command = values['command']
            cid = command['command_id']
            folder = self.folder / 'component-state' / 'command-files' / cid
            folder.mkdir(parents=True)
            items = []
            for index, source in enumerate(self.sources):
                code, digest = source['warehouse_code'], source['sha256']
                path = folder / f'{index}-{digest}.xlsx'
                shutil.copyfile(source['file'], path)
                result = {**source, 'file': str(path), 'run_id': 'run-' + code, 'account_binding': self.account,
                          'bytes': path.stat().st_size, 'task_id': 'original-' + code}
                items.append({'index': index, 'warehouse_code': code, 'state': 'SUCCEEDED',
                              'run_id': result['run_id'], 'task_id': result['task_id'], 'result': result})
            record = {'id': cid, 'request': command, 'fingerprint': fingerprint(command), 'sequence': 1,
                      'state': 'SUCCEEDED', 'items': items}
            atomic_json(self.path, record)
            return {'record': record}
        if action == 'status':
            record = read_json(self.path)
            assert record['id'] == values['command_id']
            return record
        raise AssertionError(action)


def start_smoke(app, win, args):
    from PySide6.QtCore import QTimer
    from application_runtime import current
    from scheduled_smoke import RetryPlatforms, fixture
    from scheduled_replenishment import signature
    import work_repository as wr

    out = Path(args[0]).resolve()
    marker = read_json(out / 'synthetic.json')
    if (marker.get('synthetic') is not True or marker.get('live_platform') is not False
            or Path(os.environ.get('REPLENISHMENT_DATA_DIR', '')).resolve() != out / 'settings'
            or Path(os.environ.get('LOCALAPPDATA', '')).resolve() != out / 'localappdata'):
        raise ValueError('进程恢复验收必须使用独立资料目录')
    mode = marker['mode']
    session = current().process_recovery
    manager = win.scheduled
    win.jst_generator.timer.stop()
    win.aoxiang_workflows.timer.stop()
    win.timed_workbench.timer.stop()
    status_path = session.folder / (session.owner['session'] + '.json')
    report = {'passed': False, 'frozen': bool(getattr(sys, 'frozen', False)), 'mode': mode}
    start_path = out / 'started.json'
    if not start_path.exists():
        atomic_json(start_path, {'pid': os.getpid(), 'status_path': str(status_path)})

    def finish(error=None):
        report.update(passed=error is None, pid=os.getpid(), restart_attempt=session.owner['attempt'])
        if error:
            report['error'] = error
        atomic_json(out / 'report.json', report)
        win._discard_close = True
        win.job_dirty = False
        win.close()

    def set_backend(catalogs, manifest):
        collection = RecordedCollection(out, manifest)
        manager.platform = lambda cancel: RetryPlatforms(manager.repo, catalogs, manifest, cancel, {'calls': []}, collection)

    def complete():
        try:
            prior = read_json(out / 'checkpoint.json')
            run = manager.repo.get(prior['run_id'])
            if manager.worker or run['state'] != 'COMPLETE':
                return
            timer.stop()
            stores = [entry['state'] for entry in run['stores']]
            assert stores == ['READY', 'READY'] and run['external']['ax'] == prior['command_id']
            assert signature(manager.repo.value(run['id'], 'result:A')) == prior['result_a']
            assert manager.repo.value(run['id'], 'result:B') is not None
            actions = read_json(out / 'platform-actions.json')
            assert actions.count('reserve') == 1 and actions.count('configuration') == 1
            assert actions.count('status') > prior['status_count']
            assert win.work_repository.list_jobs() == [] and win.orders.list() == []
            report.update(original_main_pid=prior['pid'], run_id=run['id'], platform_command=run['external']['ax'],
                          completed=[1, 2], reserve_calls=actions.count('reserve'), original_result_preserved=True)
            win.grab().save(str(out / '恢复后的工作台.png'))
            finish()
        except Exception:
            timer.stop()
            finish(traceback.format_exc())

    def first_run():
        try:
            plan, catalogs, manifest = fixture(out / 'settings')
            (out / 'frozen.txt').write_text(wr.dumps({'catalogs': catalogs, 'manifest': manifest}), 'utf-8')
            set_backend(catalogs, manifest)
            run = manager.repo.manual({'name': '异常续作样本', 'plan_id': plan['id'], 'plan_name': plan['name'],
                                       'stores': plan['stores'], 'kind': 'weekly', 'weekdays': list(range(7)),
                                       'time': '06:00', 'workers': 1, 'enabled': False, 'reuse_verified_bindings': True})
            save = manager.repo.save_value
            def crash_after_first_result(rid, name, value, epoch, *, store=None):
                save(rid, name, value, epoch, store=store)
                if rid == run['id'] and store == 'A':
                    original = manager.repo.get(rid)
                    assert original['state'] == 'RUNNING' and manager.repo.value(rid, 'result:B') is None
                    atomic_json(out / 'checkpoint.json', {'run_id': rid, 'command_id': original['external']['ax'],
                                'result_a': signature(manager.repo.value(rid, 'result:A')), 'pid': os.getpid(),
                                'status_count': read_json(out / 'platform-actions.json').count('status')})
                    os._exit(23)
            manager.repo.save_value = crash_after_first_result
            manager.start(run)
            def unexpected_finish():
                if manager.worker is None and not (out / 'checkpoint.json').exists():
                    finish(f"原运行未达到模拟崩溃断点：{manager.repo.get(run['id'])['message']}")
                else:
                    QTimer.singleShot(250,unexpected_finish)
            QTimer.singleShot(250,unexpected_finish)
        except Exception:
            finish(traceback.format_exc())

    def initial():
        if not status_path.is_file() or read_json(status_path)['state'] != 'WATCHING' or not manager.recovered:
            return
        timer.stop()
        if mode == 'normal-close':
            finish()
        elif mode == 'crash-loop':
            os._exit(23)
        elif mode == 'disabled':
            action = next(a for bar in win.menuBar().actions() if bar.menu()
                          for a in bar.menu().actions() if a.text() == '崩溃后自动重启')
            action.trigger()
            assert not session.enabled()
            os._exit(23)
        elif mode == 'scheduled':
            first_run()

    timer = QTimer(win)
    timer.setInterval(100)
    if session.restarted:
        if mode == 'crash-loop':
            QTimer.singleShot(200, lambda: os._exit(23))
        else:
            frozen = wr.loads((out / 'frozen.txt').read_text('utf-8'))
            set_backend(frozen['catalogs'], frozen['manifest'])
            timer.timeout.connect(complete)
            timer.start()
    else:
        timer.timeout.connect(initial)
        timer.start()
