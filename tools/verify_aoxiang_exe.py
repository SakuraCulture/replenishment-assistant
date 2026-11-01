"""Run the frozen candidate locally, with isolated settings and no AX binding.

Optional upgrade input must be an existing QA settings directory. It is copied,
never opened by the app. All original tables and source files are checked after.
"""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'qa/export_071/qa-deps'))
import psutil
from aoxiang_bundle import sha, verify, inventory


def database_snapshot(path):
    if not path.exists():
        return {}
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True) as db:
        result = {}
        tables = db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        for name, schema in tables:
            quoted = '"'+name.replace('"', '""')+'"'
            digest, count = hashlib.sha256(), 0
            # The SQLite row order is part of this check; do not sort away a difference.
            cursor = db.execute('SELECT * FROM '+quoted)
            columns = [column[0] for column in cursor.description]
            for row in cursor:
                encoded = repr(row).encode('utf-8')
                digest.update(len(encoded).to_bytes(8, 'big'))
                digest.update(encoded)
                count += 1
            result[name] = dict(schema=schema, columns=columns, rows=count, values_types_sha256=digest.hexdigest())
        return result


def files_snapshot(folder):
    return {name:sha(path) for name,path in inventory(folder).items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--upgrade-from', type=Path)
    parser.add_argument('--runs', type=int, default=2)
    parser.add_argument('--business', action='store_true', help='Synthetic input/payment/receipt exercise; requires --runs 1')
    args = parser.parse_args()
    if not 1 <= args.runs <= 5:
        raise ValueError('Use 1 to 5 isolated serial runs')
    if args.business and (args.upgrade_from or args.runs != 1):
        raise ValueError('Business fixture requires a new directory and exactly one run')
    exe, out = args.exe.resolve(), args.output.resolve()
    component = exe.parent/'_internal/components/aoxiang'
    manifest = verify(component)
    info = json.loads((exe.parent/'_internal/build_info.json').read_text(encoding='utf-8'))
    if manifest['source_commit'] != info['source_commit']:
        raise ValueError('Component and EXE have different build commits')
    out.mkdir(parents=True, exist_ok=False)
    if args.business:
        from prepare_aoxiang_exe_fixture import prepare
        fixture = prepare(out,component)
    source_files, before = None, {}
    if args.upgrade_from:
        source = args.upgrade_from.resolve()
        if not source.is_relative_to((ROOT/'qa').resolve()) or source == out or source.is_relative_to(out):
            raise ValueError('Only a separate QA settings fixture may be copied')
        source_files = files_snapshot(source)
        if any(p.endswith(('-wal', '-shm')) for p in source_files):
            raise ValueError('QA fixture must be closed before copying')
        if (source/'aoxiang').exists():
            raise ValueError('Upgrade fixture must not contain a live AX binding')
        shutil.copytree(source, out/'settings')
        before = database_snapshot(out/'settings/workbench.sqlite3')
        if not before:
            raise ValueError('Upgrade fixture has no historical database')
    env = os.environ.copy()
    for name in ('PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','QT_QPA_PLATFORM'):
        env.pop(name, None)
    env.update(REPLENISHMENT_DATA_DIR=str(out/'settings'), LOCALAPPDATA=str(out/'localappdata'), PYTHONUTF8='1')
    env['PATH'] = env['WINDIR']+';'+str(Path(env['WINDIR'])/'System32')
    runs = []
    for index in range(args.runs):
        start, descendants, peak = time.perf_counter(), set(), 0
        with (out/f'process-{index+1}.txt').open('w', encoding='utf-8') as log:
            process = subprocess.Popen([str(exe),'--aoxiang-smoke',str(out)]+(['business'] if args.business else []), cwd=out, env=env, stdout=log, stderr=log)
            host = psutil.Process(process.pid)
            while process.poll() is None:
                try:
                    children = host.children(recursive=True)
                    descendants.update((child.pid,child.create_time()) for child in children)
                    peak = max(peak, sum(p.memory_info().rss for p in [host,*children] if p.is_running()))
                except psutil.Error:
                    pass
                if time.perf_counter()-start > 120:
                    # This runner only permits unconfigured local reads: no upload/commit can be active.
                    for child in reversed(host.children(recursive=True)):
                        child.terminate()
                    host.terminate()
                    process.wait(10)
                    raise TimeoutError('Owned isolated smoke process timed out')
                time.sleep(.05)
        remaining = []
        for pid, created in descendants:
            try:
                child = psutil.Process(pid)
                if child.create_time() == created and child.is_running():
                    remaining.append(pid)
            except psutil.Error:
                pass
        result = json.loads((out/'smoke.json').read_text(encoding='utf-8'))
        result.update(exit_code=process.returncode, remaining_children=remaining, sampled_descendants=len(descendants),
                      elapsed_seconds=time.perf_counter()-start, peak_tree_rss_bytes=peak)
        runs.append(result)
        (out/f'smoke-{index+1}.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        if not result['passed'] or not result['actual_exe'] or process.returncode or remaining or result['source_commit'] != info['source_commit']:
            raise RuntimeError('Actual EXE smoke failed; see '+str(out/f'smoke-{index+1}.json'))
    after = database_snapshot(out/'settings/workbench.sqlite3')
    historical_differences = [name for name,value in before.items() if after.get(name) != value]
    if historical_differences:
        raise ValueError('Existing historical tables changed: '+repr(historical_differences))
    if source_files is not None and source_files != files_snapshot(args.upgrade_from.resolve()):
        raise ValueError('Original QA settings fixture changed')
    if verify(component) != manifest:
        raise ValueError('Bundled component changed during execution')
    business_comparison = None
    if args.business:
        sys.path.insert(0,str(ROOT/'src'))
        import batch_service, engine, parameter_schema
        from parallel_qa import diff, clean, WHITELIST
        from structure_oracle import excel_snapshot
        loaded = batch_service.load([f['file'] for f in fixture['manifest']['files']],fixture['master'])
        profile = json.loads((ROOT/'src/default_must_sell.json').read_text(encoding='utf-8'))
        frozen = json.loads((out/'business-results.json').read_text(encoding='utf-8'))
        source, excel_differences = [], []
        jobs = {code:dict(warehouses=engine.DEFAULT_WAREHOUSES,exception=True,budget=budget,parameter_config=parameter_schema.standard()) for code,budget in [('W2',30),('W1',None)]}
        result_map = batch_service.run(loaded,jobs,profile,2)
        for code in jobs:
            result = result_map[code]
            source.append(result)
            engine.export_xlsx(result,out/(code+'-source.xlsx'))
            excel_differences += diff(excel_snapshot(out/(code+'-source.xlsx')),excel_snapshot(out/(code+'-frozen.xlsx')))
        # Round-trip only to normalize Python tuples to their JSON representation.
        source = json.loads(json.dumps(source,ensure_ascii=False))
        differences = diff(clean(source),clean(frozen))
        business_comparison = dict(differences=differences,excel_differences=excel_differences,whitelist=WHITELIST,
                                   scope='Frozen application vs same source, identical synthetic inputs; baseline rules verified separately')
        if differences or excel_differences:
            (out/'business-differences.json').write_text(json.dumps(business_comparison,ensure_ascii=False,indent=2),encoding='utf-8')
            raise ValueError('Frozen/source business differences')
    report = dict(passed=True, actual_exe=True, live_platform=False, exe=str(exe), exe_sha256=sha(exe),
                  build_commit=info['source_commit'], component_manifest_sha256=sha(component/'integrity.manifest.json'),
                  runs=runs, upgrade_fixture=str(args.upgrade_from) if args.upgrade_from else None,
                  historical_tables=before, historical_table_differences=historical_differences, business_comparison=business_comparison,
                  added_tables=sorted(set(after)-set(before)) if before else [],
                  original_fixture_unchanged=source_files is not None,
                  memory_method='50 ms samples; sum of RSS of app and observed descendants; shared pages may be counted more than once',
                  limitations=['No live platform query/upload/receipt; business fixtures are explicitly synthetic',
                               'Local EXE operations do not prove the real platform end-to-end chain'])
    (out/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(dict(passed=True, output=str(out), runs=len(runs), historical_tables=len(before)), ensure_ascii=False))


if __name__ == '__main__':
    main()
