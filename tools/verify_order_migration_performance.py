"""Compare frozen Windows builds through their existing native migration dialogs."""
import argparse
from collections import defaultdict
from contextlib import closing
import copy
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from statistics import median
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
from verify_order_merge import hash_file, read, seed, write
from data_location_status import read as read_status


class BaselineStatusWriteBlocked(RuntimeError):
    pass


def orders(root):
    """Read every saved revision and independently hash ordered rows per warehouse."""
    import snapshot_json as codec
    import storage_chunks
    from result_semantics import order_quantity

    with closing(sqlite3.connect((root / 'workbench.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
        result = {}
        for oid, day, number, revision, blob in db.execute('''
            SELECT n.oid,n.day,n.number,r.revision,r.payload
            FROM normal_orders n JOIN normal_revisions r ON n.oid=r.oid
            ORDER BY n.oid,r.revision
        '''):
            value = storage_chunks.load(blob, db)
            by_warehouse = defaultdict(list)
            for row in value['result']['rows']:
                by_warehouse[row['AU']].append(row)
            summary = result.setdefault(oid, {'day': day, 'number': number, 'revisions': {}})
            summary['revisions'][str(revision)] = {
                'snapshot_sha256': hashlib.sha256(codec.dumps(value).encode()).hexdigest(),
                'warehouses': {
                    warehouse: {'sku_rows': len(rows), 'quantity': sum(order_quantity(row) or 0 for row in rows),
                                'ordered_rows_sha256': hashlib.sha256(codec.dumps(rows).encode()).hexdigest()}
                    for warehouse, rows in by_warehouse.items()
                },
            }
        return result


def create_fixture(root, count, row_count):
    import normal_orders
    import order_migration
    import result_semantics
    from work_repository import now

    root.mkdir(parents=True, exist_ok=False)
    seeded = seed(root)
    source = seeded['source']
    with closing(sqlite3.connect(source.path)) as db, db:
        for index in range(count):
            code = f'LOAD{index:03}'
            oid = 'synthetic-oid-' + code
            payload = copy.deepcopy(source.payload)
            result = payload['result']
            basis = result['rows']
            result.update(store=code, store_name='隔离样本 ' + code)
            result['rows'] = [
                dict(basis[i % len(basis)], E=f'{i:013}', B=code, A=result['store_name'],
                     _meta=dict(basis[i % len(basis)]['_meta'], export_row=i + 2))
                for i in range(row_count)
            ]
            result['summary'] = result_semantics.summarize(result['rows'])
            db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',
                       (source.day, code, 'normal_model', oid, f'BH-20261113-{code}-ZC', 1))
            db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',
                       (oid, 1, normal_orders.fingerprint_owned(result), now(), normal_orders.encode(payload, db)))
    package = root / 'comparison.zip'
    exported = order_migration.export(source.root, package)
    source_orders, target_orders = orders(source.root), orders(root / 'settings')
    additions = {oid: value for oid, value in source_orders.items() if oid not in target_orders and oid != 'source-clash-identity'}
    expected = read(root / 'synthetic.json')
    expected['initial_counts']['added'] += count
    write(root / 'fixture.json', {'package_sha256': hash_file(package), 'package_bytes': package.stat().st_size,
                                'logical_orders': len(source_orders), 'load_rows': count * row_count,
                                'members': exported['manifest']['entries'], 'marker': expected,
                                'source': source_orders, 'target': target_orders,
                                'merged': target_orders | additions})


def close_progress(writer_pid):
    """Dismiss only this completed QA launch's independent progress window."""
    from application_processes import records
    from ctypes import wintypes

    pids = {row['pid'] for row in records()
            if row['parent'] == writer_pid and '--order-migration-progress' in row.get('argv', ())}
    if not pids:
        return
    user32 = ctypes.WinDLL('user32')
    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback
    def visit(window, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value in pids:
            user32.PostMessageW(window, 0x10, 0, 0)
        return True

    user32.EnumWindows(visit, 0)


def memory_peak(process):
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD)] + [
            (key, ctypes.c_size_t) for key in ('peak', 'rss', 'peak_paged', 'paged',
                                               'peak_nonpaged', 'nonpaged', 'pagefile', 'peak_pagefile')]

    value = Counters()
    value.cb = ctypes.sizeof(value)
    handle = wintypes.HANDLE(int(process._handle))
    return value.peak if ctypes.WinDLL('psapi').GetProcessMemoryInfo(handle, ctypes.byref(value), value.cb) else None


def record_operation(folder):
    for file in (folder / 'localappdata').rglob('*.jsonl'):
        if file.parent.name != 'performance':
            continue
        for line in file.read_text('utf-8').splitlines():
            row = json.loads(line)
            if row.get('kind') == 'operation_end' and row.get('operation') in ('order_migration.prepare', 'order_merge_migration.prepare'):
                return row
    return {}


def run_one(exe, folder, fixture, mode):
    from migration_status import folder as status_folder

    details = read(fixture / 'fixture.json')
    package = fixture / 'comparison.zip'
    folder.mkdir()
    shutil.copytree(fixture / 'settings', folder / 'settings')
    shutil.copyfile(package, folder / 'orders.zip')
    if hash_file(folder / 'orders.zip') != details['package_sha256']:
        raise AssertionError('Comparison input changed')
    write(folder / 'synthetic.json', details['marker'])
    trace = folder / 'trace'
    environment = {**os.environ, 'REPLENISHMENT_DATA_DIR': str(folder / 'settings'),
                   'REPLENISHMENT_IO_TRACE': str(trace), 'LOCALAPPDATA': str(folder / 'localappdata'),
                   'APPDATA': str(folder / 'roaming'), 'QT_QPA_PLATFORM': 'windows', 'PYTHONUTF8': '1'}
    for key in ('PYTHONPATH', 'PYTHONHOME', 'QT_PLUGIN_PATH', 'REPLENISHMENT_PORTABLE_PATHS'):
        environment.pop(key, None)
    flag = '--order-merge-smoke' if mode == 'merge' else '--migration-shutdown-smoke'
    status_root = status_folder(folder / 'settings')
    confirmed = None
    with (folder / 'process.log').open('wb') as log:
        started = time.perf_counter()
        process = subprocess.Popen([str(exe), flag, str(folder), 'confirm'], env=environment, cwd=ROOT,
                                   stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
        while process.poll() is None:
            if time.perf_counter() - started > 150:
                raise TimeoutError(f'Native migration timed out in isolated directory {folder}')
            if confirmed is None and (folder / 'confirm-events.jsonl').is_file():
                # The test posts confirmation immediately after this event. An
                # append-only log avoids opening rc36's live atomic status file.
                events = (folder / 'confirm-events.jsonl').read_text('utf-8', errors='replace')
                marker = 'preview_verified' if mode == 'merge' else '核对完整恢复范围'
                if marker in events:
                    confirmed = time.perf_counter() - started
            time.sleep(.05)
        finished = time.perf_counter() - started
        peak_rss = memory_peak(process)
    nonce = read_status(status_root / 'latest.json')['nonce']
    receipt = read_status(status_root / (nonce + '.status.json'))
    if process.returncode or receipt['state'] != 'COMPLETED':
        close_progress(process.pid)
        error = receipt.get('error', {}).get('message', '')
        if (receipt['state'] == 'FAILED' and receipt['phase'] == 'COPYING' and '[WinError 5]' in error
                and '.status.json' in error and not Path(receipt['backup']).exists()
                and not (folder / '.settings.order-import.json').exists()
                and orders(folder / 'settings') == details['target']):
            raise BaselineStatusWriteBlocked(str(folder))
        raise AssertionError((folder, process.returncode, receipt))
    close_progress(process.pid)
    if orders(Path(receipt['backup'])) != details['target']:
        raise AssertionError('Original target backup differs')
    actual = orders(folder / 'settings')
    expected = details['merged' if mode == 'merge' else 'source']
    if actual != expected:
        raise AssertionError((folder, [oid for oid in actual.keys() | expected.keys() if actual.get(oid) != expected.get(oid)]))
    write(folder / 'order-comparison.json', {'passed': True, 'orders': actual})
    elapsed = defaultdict(float)
    for file in trace.glob('*.jsonl'):
        rows = [json.loads(line) for line in file.read_text('utf-8').splitlines()]
        scope = 'prepare' if any(row['stage'] == 'migration.prepare.total' for row in rows) else 'apply'
        for row in rows:
            if row['stage'].startswith('migration.') and row['ok']:
                phase = row['stage']
                if phase.startswith(('migration.verify.', 'migration.sqlite.')):
                    phase = scope + '.' + phase
                elapsed[phase] += row['seconds']
            if row['stage'] == 'migration.prepare.total':
                scope = 'apply'
    native = record_operation(folder)
    preflight = native.get('seconds', elapsed.get('migration.prepare.total'))
    apply_ui = finished - confirmed if confirmed is not None else None
    result = {'exe': str(exe), 'mode': mode, 'package_sha256': details['package_sha256'],
              'orders_verified': len(actual), 'prepared_seconds': preflight, 'confirm_to_terminal_seconds': apply_ui,
              'launch_to_exit_seconds': finished, 'phases': dict(elapsed),
              'peak_rss_bytes': peak_rss, 'sampled_temporary_peak_bytes': None,
              'stage_receipt': receipt['state'], 'nonce': nonce, 'native_prepare_log': native}
    write(folder / 'timing.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', required=True, type=Path)
    parser.add_argument('--seed', action='store_true')
    parser.add_argument('--orders', type=int, default=22)
    parser.add_argument('--rows', type=int, default=1800)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--exe', action='append', default=[])
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    fixture = args.fixture.resolve()
    if args.seed:
        create_fixture(fixture, args.orders, args.rows)
        return
    if not args.out or not args.exe or os.name != 'nt':
        parser.error('Supply an output directory and --exe name=path on Windows')
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=args.resume)
    builds = {key: Path(value).resolve(strict=True) for key, _, value in (item.partition('=') for item in args.exe)}
    results = read(out / 'runs.json') if args.resume else []
    expected_hash = read(fixture / 'fixture.json')['package_sha256']
    for row in results:
        if (row['package_sha256'] != expected_hash or Path(row['exe']) != builds.get(row['build'])
                or row['stage_receipt'] != 'COMPLETED'):
            raise ValueError('Existing evidence does not match the requested inputs')
    completed = {(row['round'], row['mode'], row['build']) for row in results}
    for index in range(1, args.rounds + 1):
        for mode in ('replace', 'merge'):
            for label, exe in (list(builds.items()) if index % 2 else reversed(list(builds.items()))):
                if (index, mode, label) in completed:
                    continue
                print(index, mode, label, flush=True)
                run_name = f'{index}-{mode}-{label}'
                if (out / run_name).exists():
                    run_name += '-resumed'
                try:
                    result = run_one(exe, out / run_name, fixture, mode)
                except BaselineStatusWriteBlocked as failure:
                    if not label.startswith('rc36'):
                        raise
                    print('Legacy status publication blocked, original target verified:', failure, flush=True)
                    result = run_one(exe, out / (run_name + '-retry'), fixture, mode)
                    result['earlier_failed_attempt'] = str(failure)
                results.append({'round': index, 'build': label, **result})
                write(out / 'runs.json', results)
    summaries = {}
    for label in builds:
        for mode in ('replace', 'merge'):
            runs = [row for row in results if row['mode'] == mode and row['build'] == label]
            keys = ('prepared_seconds', 'confirm_to_terminal_seconds', 'launch_to_exit_seconds', 'peak_rss_bytes', 'sampled_temporary_peak_bytes')
            phases = set().union(*(row['phases'] for row in runs))
            summaries[f'{label}/{mode}'] = {
                **{key: median(row[key] for row in runs if row[key] is not None) if any(row[key] is not None for row in runs) else None for key in keys},
                'phases': {phase: median(row['phases'].get(phase, 0) for row in runs) for phase in sorted(phases)},
                'exe_sha256': hash_file(builds[label]),
            }
    write(out / 'report.json', {'passed': True, 'platform_submissions': 0, 'summaries': summaries, 'runs': results})


if __name__ == '__main__':
    main()
