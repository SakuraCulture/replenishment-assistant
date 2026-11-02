"""AX06-12 local acceptance against fixed AX01 business baseline. Never connects AX."""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from verify_aoxiang_protocol import ROOT, git, sha
from parallel_qa import read, clean, diff, WHITELIST
from structure_oracle import excel_snapshot, source_hashes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qa', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--stage', choices=('creation', 'receipts', 'workflows', 'binding', 'delivery'), default='creation')
    args = parser.parse_args()
    qa, output = args.qa.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    baseline = json.loads((qa / 'baseline.json').read_text(encoding='utf-8'))
    protected = {}
    for key, base in [('component_runtime_sha256', ROOT / 'components/aoxiang'), ('input_sha256', qa), ('core_sha256', ROOT), ('protected', Path())]:
        for name, expected in baseline[key].items():
            if sha(base / name) != expected:
                raise ValueError('Protected file changed: ' + name)
            protected[str(base / name)] = expected
    if sha(qa / 'baseline-source.zip') != baseline['source_archive_sha256']:
        raise ValueError('Baseline source archive changed')
    with zipfile.ZipFile(qa / 'baseline-source.zip') as archive:
        originals = [n for n in archive.namelist() if n.startswith('src/') and not n.endswith('/')]
        changed = [n for n in originals if hashlib.sha256(archive.read(n)).hexdigest() != sha(ROOT / n)]
    # Explicit UI integration surfaces, not a business-field exclusion list.
    allowed = ['src/application_window.py', 'src/import_templates.py', 'src/lifecycle_ui.py']
    if args.stage in ('workflows', 'binding', 'delivery'):
        allowed.append('src/lifecycle_home_ui.py')
    if args.stage == 'delivery':
        allowed += ['src/app.py', 'src/version.json']
        with zipfile.ZipFile(qa/'baseline-source.zip') as archive:
            original_version=json.loads(archive.read('src/version.json'))
        candidate_version=json.loads((ROOT/'src/version.json').read_text(encoding='utf-8'))
        if candidate_version != dict(original_version, version='0.24.0'):
            raise ValueError('Only application version may change for delivery; model/storage/routing stay fixed')
    if set(changed) != set(allowed):
        raise ValueError('Unexpected baseline source modification: ' + repr(changed))
    env = dict(os.environ, PYTHONUTF8='1', PYTHONHASHSEED='0', QT_QPA_PLATFORM='offscreen',
               REPLENISHMENT_DATA_DIR=str(output / 'settings'), LOCALAPPDATA=str(output / 'component-settings'))
    commands = [
        ('environment', [sys.executable, 'tools/check_environment.py', '--report', str(output / 'environment.json')], ROOT),
        ('component', [str(ROOT / 'components/aoxiang/runtime/node.exe'), '--test', 'test/*.test.mjs'], ROOT / 'components/aoxiang'),
        ('python', [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'], ROOT),
        ('capture', [sys.executable, 'tools/parallel_qa.py', 'capture', '--qa', str(qa), '--source', str(ROOT), '--output', str(output / 'capture')], ROOT),
    ]
    runs = []
    for name, command, cwd in commands:
        started = time.perf_counter()
        log = output / (name + '.txt')
        with log.open('x', encoding='utf-8', newline='\n') as stream:
            r = subprocess.run(command, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT)
        runs.append({'name': name, 'command': command, 'exit_code': r.returncode, 'seconds': time.perf_counter()-started, 'log_sha256': sha(log)})
        print(name + ': ' + str(r.returncode), flush=True)
        if r.returncode:
            raise SystemExit('Failed: ' + str(log))
    py, js = [(output / (n + '.txt')).read_text(encoding='utf-8') for n in ('python', 'component')]
    if not py.rstrip().endswith('OK') or 'fail 0' not in js or 'skipped 0' not in js:
        raise ValueError('Tests incomplete or skipped')
    before, after = qa / 'baseline-capture', output / 'capture'
    names = json.loads((before / 'capture.json').read_text(encoding='utf-8'))['cases']
    actual = json.loads((after / 'capture.json').read_text(encoding='utf-8'))
    if actual['cases'] != names or actual['source_hashes'] != source_hashes(ROOT):
        raise ValueError('Capture input or source identity changed')
    comparisons = []
    for name in names:
        changes = diff(clean(read(before / (name + '.pklz'))), clean(read(after / (name + '.pklz'))))
        comparisons.append({'case': name, 'differences': len(changes), 'details': changes})
    for file in before.glob('*.xlsx'):
        changes = diff(excel_snapshot(file), excel_snapshot(after / file.name))
        comparisons.append({'case': file.name, 'differences': len(changes), 'details': changes})
    changed_protected = [n for n, h in protected.items() if sha(n) != h]
    total = sum(c['differences'] for c in comparisons)
    report = {'task': {'creation':'AX06-AX09-local-procurement','receipts':'AX10-AX12-local-receipts','workflows':'AX13-local-workflows','binding':'AX09-local-existing-purchase-binding','delivery':'AX15-local-delivery-source'}[args.stage], 'passed_local': not total and not changed_protected,
              'baseline_commit': baseline['baseline_commit'], 'head_before_commit': git('rev-parse', 'HEAD'), 'branch': git('branch', '--show-current'),
              'python_tests': int(re.search(r'Ran (\d+) tests', py)[1]), 'component_tests': int(re.search(r'tests (\d+)', js)[1]),
              'runs': runs, 'comparisons': comparisons, 'total_differences': total, 'strict_order_and_types': True,
              'dynamic_whitelist': WHITELIST, 'changed_protected': changed_protected, 'protected_files_checked': len(protected),
              'changed_original_application_files': changed, 'unchanged_original_application_files': len(originals)-len(changed),
              'core_sha256': baseline['core_sha256'], 'candidate_source_sha256': source_hashes(ROOT),
              'new_local_checks': ['exact native template package -> real Node reservation -> restart / cancel / no-browser failure',
                                   'persistent purchase identity, pause before/during upload, unknown acceptance never replayed, late receipt recovery',
                                   'per-SKU native main/detail amount checks including null vs zero and equal-total swaps',
                                   'isolated Edge native file input and confirm with locally fulfilled response; external traffic denied',
                                   'Qt preview/recovery buttons, default cancel, old task state protection and safe close'],
              'limitations': ['This local regression run makes no platform calls; live integration evidence is recorded separately.',
                              'Live multi-SKU import task counts, supplier behaviour, marker preservation and main/detail checks still require acceptance.',
                              'No EXE or live receipt acceptance claimed. Batch scheduling, periodic monitoring, manual proof-assisted binding and integration packaging remain subsequent work.',
                              'Suite durations are test execution times, not platform performance measurements.']}
    if args.stage in ('receipts', 'workflows', 'binding', 'delivery'):
        report['new_local_checks'] += [
            'D16 terminal latch and final-sync pending survive failed D17, restart and retry of original export tasks',
            'fresh observation does not reuse previous completed export; crash after proof save resumes exact file',
            'actual native source row, blank/zero distinction, SKU coverage, state and quantity/amount conflict',
            'existing lifecycle commit used for final receipt, lost acknowledgement idempotency and manual receipt protection',
            'payment change, original file tamper, cancellation and concurrent lifecycle update reject formal write',
            'focused Qt receipt page: local-only open, exact task stop/resume, safe close and final-save retry']
    if args.stage in ('workflows', 'binding', 'delivery'):
        report['new_local_checks'] += [
            'ordered persistent batch preparation, frozen per-order preview and explicit selection-bound upload confirmation',
            'periodic receipt opt-in, due-order exclusion, terminal latch, final-save retry and post-queue disable',
            'foreground handoff waits for actual component exit while Qt heartbeat remains responsive',
            'same-directory scheduler lock, targeted stop, safe app close, original-task restart and initialization failure',
            'real original lifecycle commit through batch runner, lost-ack retry adds no receipt version']
        report['limitations'][2] = 'No EXE or live platform acceptance claimed. Manual proof-assisted binding, complete integration migration and candidate packaging remain subsequent work.'
    if args.stage in ('binding', 'delivery'):
        report['new_local_checks'] += [
            'existing PO native proof shares automatic per-SKU checks; explicit operator confirmation binds exact preview',
            'unknown upload requires original marker uniqueness; known receipt and multiple-task conflicts cannot be bypassed',
            'source tamper, concurrent preview/payment change, duplicate PO, pause/restart, lost confirmation and late receipt',
            'completed existing PO latches final-sync pending; UI shows actual platform status and distinguishes missing status',
            'malformed business form receives correlated validation error; same real Node process remains usable',
            'accepted host close stops timers and rejects queued callbacks; declined close keeps workflows available']
        report['limitations'][2] = 'No EXE or live platform acceptance claimed. Complete integration migration and candidate packaging remain subsequent work; live existing-PO proof must still be accepted when user resumes integration.'
    if args.stage == 'delivery':
        report['new_local_checks'] += ['component bundle exact dependency lock, source-only whitelist and checksum verification']
        report['limitations'][2] = 'This report proves source regression only. Actual EXE, upgrade compatibility and live platform evidence are separate. Full cross-machine migration is outside the narrowed AX01-AX15 scope and is not claimed supported.'
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    print(json.dumps({k:report[k] for k in ('passed_local','python_tests','component_tests','total_differences')}, ensure_ascii=False))
    if not report['passed_local']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
