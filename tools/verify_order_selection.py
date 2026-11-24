"""Native order-scope export, add-only import, replay protection and recovery."""
import argparse
from pathlib import Path
import os
import shutil
import subprocess
import sys

from verify_order_migration_performance import add_load, close_progress, orders, run_one
from verify_order_merge import hash_file, read, table, write
from merge_fixture import Order, second_order, shared_purchase
from migration_status import latest
from order_migration import FORMAT, SELECTION_FORMAT, journal, manifest


def run(exe, case, flag, operation, expected_code=0):
    environment = {**os.environ, 'REPLENISHMENT_DATA_DIR': str(case / 'settings'),
                   'LOCALAPPDATA': str(case / 'localappdata'), 'APPDATA': str(case / 'roaming'),
                   'QT_QPA_PLATFORM': 'windows', 'PYTHONUTF8': '1'}
    for key in ('PYTHONPATH', 'PYTHONHOME', 'QT_PLUGIN_PATH', 'REPLENISHMENT_PORTABLE_PATHS'):
        environment.pop(key, None)
    with (case / f'{operation}.log').open('wb') as log:
        process = subprocess.Popen([str(exe), flag, str(case), operation], env=environment,
                                   stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
        code = process.wait(timeout=145)
    if flag == '--order-merge-smoke':
        close_progress(process.pid)
    assert code == expected_code, (case.name, operation, code)


def export_ui(exe, case, source, mode, marker):
    case.mkdir()
    shutil.copytree(source, case / 'settings')
    write(case / 'synthetic.json', marker)
    before = orders(case / 'settings')
    run(exe, case, '--order-selection-smoke', mode)
    result = read(case / 'selection-ui.json')
    assert result['passed'] and result['frozen'], result
    assert orders(case / 'settings') == before
    return result


def receiving_fixture(folder, target, package, expected, marker):
    folder.mkdir()
    shutil.copytree(target, folder / 'settings')
    shutil.copyfile(package, folder / 'comparison.zip')
    original = orders(target)
    write(folder / 'fixture.json', {'target': original, 'merged': original | expected, 'marker': marker,
                                   'package_sha256': hash_file(package)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--exe', required=True, type=Path)
    args = parser.parse_args()
    out, exe = args.out.resolve(), args.exe.resolve(strict=True)
    assert os.name == 'nt'
    out.mkdir(parents=True, exist_ok=False)
    source = Order(out / 'source', code='FIRST')
    source.complete(stock=7)
    peer = second_order(source, source.code, oid='cross-day-shared', day='2026-11-15')
    shared_purchase(source, [source.h, peer])
    add_load(source, 15, 1200)
    local = Order(out / 'target', code='LOCAL')
    before, all_orders = orders(local.root), orders(source.root)
    selected = {oid: all_orders[oid] for oid in (source.oid, peer['oid'])}
    marker = {'synthetic': True, 'live_platform': False, 'selected_number': source.number,
              'business_day': peer['day'], 'expected': {source.number: True, all_orders[peer['oid']]['number']: False}}
    results = []
    for mode in ('checked', 'by-date', 'full'):
        inputs = marker if mode != 'by-date' else {**marker, 'expected': {key: not value for key, value in marker['expected'].items()}}
        proof = export_ui(exe, out / mode, source.root, mode, inputs)
        archive = out / mode / 'orders.zip'
        packet = manifest(archive)
        expected_count = len(all_orders) if mode == 'full' else len(selected)
        assert packet['counts']['normal_orders'] == expected_count
        assert packet['format'] == (FORMAT if mode == 'full' else SELECTION_FORMAT)
        if mode != 'full':
            assert set(packet['selection']['included']) == set(selected)
            assert packet['selection']['requested'] == [source.oid if mode == 'checked' else peer['oid']]
        results.append({**proof, 'bytes': archive.stat().st_size, 'package_sha256': hash_file(archive), 'parents': expected_count})

    package = out / 'checked/orders.zip'
    rejected = out / 'reject-restore'
    rejected.mkdir()
    shutil.copytree(local.root, rejected / 'settings')
    shutil.copyfile(package, rejected / 'orders.zip')
    write(rejected / 'synthetic.json', marker)
    run(exe, rejected, '--order-selection-smoke', 'reject-restore')
    reject_proof = read(rejected / 'selection-ui.json')
    assert reject_proof['passed'] and not journal(rejected / 'settings').exists() and orders(rejected / 'settings') == before
    results.append(reject_proof)

    def merge_marker(count):
        return {**marker, 'initial_counts': {'added': count, 'duplicate': 0, 'conflict': 0, 'deleted': 0},
                'repeat_counts': {'added': 0, 'duplicate': count, 'conflict': 0, 'deleted': 0}, 'reopen_parent_count': 1 + count}

    for name, expected in (('full', all_orders), ('checked', selected), ('by-date', selected)):
        seed = out / ('input-' + name)
        receiving_fixture(seed, local.root, out / name / 'orders.zip', expected, merge_marker(len(expected)))
        proof = run_one(exe, out / ('import-' + name), seed, 'merge')
        results.append({'mode': 'import-' + name, 'passed': True, **proof})

    installed = out / 'import-checked'
    for table_name in ('lifecycle_versions', 'purchase_documents', 'purchase_versions', 'jst_generations', 'purchase_files', 'lifecycle_files'):
        source_rows = table(source.path, table_name)
        assert source_rows and table(installed / 'settings/workbench.sqlite3', table_name) == source_rows, table_name
    auth = latest(installed / 'settings')
    run(exe, installed, '--order-merge-smoke', 'repeat')
    assert orders(installed / 'settings') == before | selected
    assert len(list(installed.glob('settings.订单导入备份-*'))) == 1 and Path(auth['backup']).is_dir()
    results.append({'mode': 'repeat', 'passed': True, 'added': 0})

    for mode, code, expected in (('interrupt-before', 73, before), ('interrupt-after', 74, before | selected)):
        case = out / mode
        case.mkdir()
        shutil.copytree(local.root, case / 'settings')
        shutil.copyfile(package, case / 'orders.zip')
        case_marker = merge_marker(len(selected))
        case_marker['reopen_parent_count'] = len(expected)
        write(case / 'synthetic.json', case_marker)
        run(exe, case, '--order-merge-smoke', mode, code)
        assert journal(case / 'settings').is_file()
        run(exe, case, '--order-merge-smoke', 'reopen-after-selection')
        receipt = latest(case / 'settings')
        assert receipt['state'] == ('FAILED' if mode == 'interrupt-before' else 'COMPLETED')
        assert not journal(case / 'settings').exists() and orders(case / 'settings') == expected
        results.append({'mode': mode, 'passed': True, 'writer_exit': code, 'recovered': receipt['state'], 'parents': len(expected)})

    write(out / 'report.json', {'passed': True, 'exe_sha256': hash_file(exe), 'scope_evidence': selected, 'cases': results, 'live_platform': False})


if __name__ == '__main__':
    main()
