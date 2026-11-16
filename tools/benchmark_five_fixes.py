"""Alternating frozen/new worker benchmarks on the same synthetic business snapshots."""
import argparse
import copy
import gc
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from statistics import median


def rss(pid=None):
    """Native Windows working set; sample the actual worker, not its launcher."""
    import ctypes as c
    from ctypes import wintypes as w
    class Counters(c.Structure):
        _fields_ = [('cb', w.DWORD), ('faults', w.DWORD)] + [(n, c.c_size_t) for n in
            ('peak', 'working', 'peak_paged', 'paged', 'peak_nonpaged', 'nonpaged', 'pagefile', 'peak_pagefile')]
    kernel = c.WinDLL('kernel32', use_last_error=True);api = c.WinDLL('psapi', use_last_error=True)
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD];kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    api.GetProcessMemoryInfo.argtypes = [w.HANDLE, c.POINTER(Counters), w.DWORD]
    handle = kernel.GetCurrentProcess() if pid is None else kernel.OpenProcess(0x1010, False, pid)
    if not handle:raise c.WinError(c.get_last_error())
    try:
        value = Counters();value.cb = c.sizeof(value)
        if not api.GetProcessMemoryInfo(handle, c.byref(value), value.cb):raise c.WinError(c.get_last_error())
        return value.working
    finally:
        if pid is not None:kernel.CloseHandle(handle)


def worker(a):
    variant = a.variant.resolve()
    sys.path[:0] = [str(variant / 'src'), str(variant / 'tests')]
    import order_lifecycle as lc, purchases, work_repository as wr
    import aoxiang_purchase as purchase, aoxiang_purchase_tasks as tasks
    run = a.run.resolve();shutil.copytree(a.fixture / 'settings', run / 'settings')
    path = run / 'settings/workbench.sqlite3'
    head = next(h for h in purchases.targets(path) if h['warehouse'] == '河北仓')
    values = {};starts = []
    def measure(name, call):
        gc.collect();before = rss()
        print(json.dumps({'start': name, 'rss': before, 'worker_pid': os.getpid()}), flush=True)
        t = time.perf_counter();value = call();elapsed = time.perf_counter() - t
        values[name] = {'seconds': elapsed, 'rss_before_mib': before / 1048576, 'rss_after_mib': rss() / 1048576}
        print(json.dumps({'end': name}), flush=True)
        return value
    view = measure('light_snapshot', lambda: lc.snapshot(path, head, _comparison=False))
    config = {'configured': True, 'enabled': True, 'account_binding': {'account_id': 'A', 'merchant_code': 'M'},
              'store_mapping_version': 'a' * 64, 'targets': [{'buyerWarehouseCode': head['store'], 'displayName': '测试店'}]}
    suppliers = {'河北仓': '2078246', '山东仓': 'ZXC2025'}
    plan = measure('freeze_payment', lambda: purchase.freeze_payment(path, head, config, suppliers))
    def check(db=None):
        if a.new:purchase.validate_basis(path, plan, config, suppliers, _db=db)
        else:purchase.validate_basis(path, plan, config, suppliers)
        return tasks.check_admission(path, plan, **({'db': db} if a.new else {}))
    if a.new:
        import purchase_dispatch
        original = lc.begin
        def timed_begin(*args, **kwargs):
            value = original(*args, **kwargs);starts.append(time.perf_counter());return value
        lc.begin = timed_begin
        intent = {'id': 'benchmark', 'basis': {k: head[k] for k in ('oid', 'revision', 'warehouse', 'store', 'number')},
                  'identity': {'accountId': 'A', 'merchantCode': 'M'}, 'mappingVersion': 'a' * 64, 'file': {'sha256': 'b' * 64}}
        measure('validate_upload_basis', lambda: purchase_dispatch.authorize(path, intent, check))
        values['validate_upload_basis']['write_hold_seconds'] = time.perf_counter() - starts[-1]
    else:measure('validate_upload_basis', check)
    digest = hashlib.sha256(wr.dumps({'view': view, 'plan': plan}).encode()).hexdigest()
    assert not view['rows'] and view['health']['payment']
    print(json.dumps({'result': True, 'values': values, 'digest': digest, 'payment_rows': len(plan['rows']),
                      'quantity': plan['total_quantity'], 'amount': plan['total_amount']}), flush=True)


def fixture(folder, count, root):
    sys.path[:0] = [str(root / 'src'), str(root / 'tests')]
    import test_lifecycle as fixtures
    import normal_orders as normal, purchases, result_semantics as semantics, warehouse_catalog
    from test_jst import catalog
    f = fixtures.LifecycleTests();f.setUp()
    try:
        payload = copy.deepcopy(f.payload);template = payload['result']['rows'][0]
        payload['result']['rows'] = [dict(copy.deepcopy(template), E=f'SYN{i:06d}') for i in range(count)]
        payload['result']['summary'] = semantics.summarize(payload['result']['rows'])
        digest = normal.fingerprint_owned(payload['result'], payload.get('new_focus'))
        with closing(f.work.connect()) as db, db:
            db.execute('UPDATE normal_revisions SET digest=?,payload=? WHERE oid=? AND revision=1', (digest, normal.encode(payload, db), 'oid'))
        f.h = next(h for h in purchases.targets(f.path) if h['warehouse'] == '河北仓')
        rows = [{'商品编码': r['E'], '门店确认数量': 5} for r in payload['result']['rows']]
        f.save('return', rows)
        rows = [{'商品编码': r['E'], '付款单商品数量': 3, '实际金额': 9} for r in payload['result']['rows']]
        f.save('payment', rows)
        data = catalog();item = next(iter(data['items'].values()))
        data['items'] = {r['E']: dict(copy.deepcopy(item), 商品编码=r['E'], 商品标签='') for r in payload['result']['rows']}
        data['revision'] = warehouse_catalog.revision(data['items']);warehouse_catalog.Repository(f.path.parent).save(data)
        folder.mkdir();shutil.copytree(f.path.parent, folder / 'settings')
    finally:f.tearDown()


def main(a):
    root = Path(__file__).resolve().parents[1];out = a.out.resolve();out.mkdir(exist_ok=False)
    for count in a.sizes:fixture(out / f'fixture-{count}', count, root)
    results = []
    for repeat in range(3):
        order = [('old', a.baseline), ('new', root)] if repeat % 2 == 0 else [('new', root), ('old', a.baseline)]
        for count in a.sizes:
            for label, variant in order:
                run = out / f'{label}-{count}-{repeat}';run.mkdir()
                cmd = [sys.executable, str(Path(__file__).resolve()), '--worker', '--variant', str(variant), '--fixture', str(out / f'fixture-{count}'), '--run', str(run)]
                if label == 'new':cmd.append('--new')
                with (run / 'stderr.log').open('wb') as stderr:
                    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=stderr, text=True, encoding='utf-8')
                    state = {'phase': None, 'pid': None};peaks = {};frames = []
                    def reader():
                        for line in p.stdout:
                            v = json.loads(line);frames.append(v)
                            if v.get('start'):
                                state['pid'] = v['worker_pid'];state['phase'] = v['start'];peaks[v['start']] = v['rss']
                            if v.get('end'):state['phase'] = None
                    t = threading.Thread(target=reader);t.start()
                    while p.poll() is None:
                        try:
                            phase = state['phase'];pid = state['pid']
                            if phase and pid:
                                sample = rss(pid)
                                peaks[phase] = max(peaks.get(phase, 0), sample)
                        except OSError:pass
                        time.sleep(.01)
                    t.join();assert p.returncode == 0, (run, (run / 'stderr.log').read_text(encoding='utf-8'))
                value = next(v for v in frames if v.get('result'))
                for name, metric in value['values'].items():metric['sampled_peak_worker_rss_mib'] = max(peaks.get(name, 0) / 1048576, metric['rss_after_mib'])
                results.append({'variant': label, 'count': count, 'round': repeat, **value})
                (out / 'runs.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps({'completed': len(results), 'variant': label, 'count': count, 'round': repeat}), flush=True)
    summary = {}
    for count in a.sizes:
        chosen = [r for r in results if r['count'] == count]
        assert len({r['digest'] for r in chosen}) == 1
        group = {'outputs_equal': True, 'digest': chosen[0]['digest'], 'metrics': {}}
        for name in chosen[0]['values']:
            group['metrics'][name] = {label: {k: median(r['values'][name][k] for r in chosen if r['variant'] == label) for k in next(r for r in chosen if r['variant'] == label)['values'][name]} for label in ('old', 'new')}
        summary[str(count)] = group
    report = {'passed': True, 'cases': summary, 'repeats': 3, 'runs': results, 'synthetic': True, 'live_platform': False,
              'notes': 'Same synthetic DB per size; old/new alternate. RSS sampled from actual worker PID each 10ms. Upload-basis comparison includes new short host approval; not full browser/upload latency. No business-PC capacity claim.'}
    (out / 'comparison.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    p = argparse.ArgumentParser();p.add_argument('--baseline', type=Path);p.add_argument('--out', type=Path);p.add_argument('--sizes', type=int, nargs='+', default=[1000, 5000, 10000]);p.add_argument('--worker', action='store_true');p.add_argument('--variant', type=Path);p.add_argument('--fixture', type=Path);p.add_argument('--run', type=Path);p.add_argument('--new', action='store_true')
    a = p.parse_args();worker(a) if a.worker else main(a)
