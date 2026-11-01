"""AX04/05 local acceptance; live platform and EXE evidence are explicitly separate."""
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
    args = parser.parse_args()
    qa, output = args.qa.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    baseline = json.loads((qa / 'baseline.json').read_text(encoding='utf-8'))
    protected = {}
    for key, base in [('component_runtime_sha256', ROOT / 'components/aoxiang'), ('input_sha256', qa),
                      ('core_sha256', ROOT), ('protected', Path())]:
        for name, expected in baseline[key].items():
            if sha(base / name) != expected:
                raise ValueError('Protected file changed: ' + name)
            protected[str(base / name)] = expected
    if sha(qa / 'baseline-source.zip') != baseline['source_archive_sha256']:
        raise ValueError('Frozen baseline source changed')
    changed_original = []
    with zipfile.ZipFile(qa / 'baseline-source.zip') as archive:
        for name in archive.namelist():
            if name.startswith('src/') and not name.endswith('/'):
                if hashlib.sha256(archive.read(name)).hexdigest() != sha(ROOT / name):
                    changed_original.append(name)
    if set(changed_original) != {'src/application_window.py', 'src/import_templates.py'}:
        raise ValueError('Unexpected original application changes: ' + repr(changed_original))
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
            result = subprocess.run(command, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT)
        runs.append({'name': name, 'command': command, 'exit_code': result.returncode, 'seconds': time.perf_counter() - started, 'log_sha256': sha(log)})
        print(name + ': ' + str(result.returncode), flush=True)
        if result.returncode:
            raise SystemExit('Failed: ' + str(log))
    py = (output / 'python.txt').read_text(encoding='utf-8')
    js = (output / 'component.txt').read_text(encoding='utf-8')
    if not py.rstrip().endswith('OK') or 'fail 0' not in js or 'skipped 0' not in js:
        raise ValueError('Tests incomplete or skipped')
    before, after = qa / 'baseline-capture', output / 'capture'
    names = json.loads((before / 'capture.json').read_text(encoding='utf-8'))['cases']
    capture = json.loads((after / 'capture.json').read_text(encoding='utf-8'))
    if names != capture['cases'] or capture['source_hashes'] != source_hashes(ROOT):
        raise ValueError('Capture cases or current source hashes changed')
    comparisons = []
    for name in names:
        changes = diff(clean(read(before / (name + '.pklz'))), clean(read(after / (name + '.pklz'))))
        comparisons.append({'case': name, 'differences': len(changes), 'details': changes})
    for file in before.glob('*.xlsx'):
        changes = diff(excel_snapshot(file), excel_snapshot(after / file.name))
        comparisons.append({'case': file.name, 'differences': len(changes), 'details': changes})
    changed_protected = [name for name, expected in protected.items() if sha(name) != expected]
    total = sum(c['differences'] for c in comparisons)
    report = {'task': 'AX04/AX05-local', 'passed_local': not total and not changed_protected,
              'baseline_commit': baseline['baseline_commit'], 'head_before_commit': git('rev-parse', 'HEAD'),
              'branch': git('branch', '--show-current'), 'python_tests': int(re.search(r'Ran (\d+) tests', py)[1]),
              'component_tests': int(re.search(r'tests (\d+)', js)[1]), 'runs': runs,
              'comparisons': comparisons, 'total_differences': total, 'strict_order_and_types': True,
              'dynamic_whitelist': WHITELIST, 'changed_protected': changed_protected,
              'changed_original_application_files': changed_original,
              'core_sha256': baseline['core_sha256'], 'candidate_source_sha256': source_hashes(ROOT),
              'new_path_acceptance': 'test_aoxiang_input_integration uses actual Qt Worker, two synthetic per-store Excel originals, source archive, calculation and Excel export against manual input with identical files.',
              'live_platform': {'download_passed': False, 'status': 'User deferred further live integration after Edge connection authorization timeout; no export task was submitted.',
                                'preserved_command': 'rep-dde384b54a9a4cdaaca2916fb67f010f', 'evidence': str(qa / 'ax04-live-fetch')},
              'limitations': ['Actual platform export/download and EXE acceptance pending; not claimed complete.',
                              'Test-suite durations are not download performance measurements.',
                              'Procurement and receipt integration remain subsequent phases.']}
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    print(json.dumps({k: report[k] for k in ['passed_local', 'python_tests', 'component_tests', 'total_differences']}, ensure_ascii=False))
    if not report['passed_local']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
