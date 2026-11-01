"""Repeat AX02 protocol checks against the immutable AX01 manifest in fresh QA directories."""
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

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, encoding='utf-8').strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    baseline_path, output = args.baseline.resolve(), args.output.resolve()
    manifest = json.loads(baseline_path.read_text(encoding='utf-8'))
    output.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ, PYTHONUTF8='1', PYTHONHASHSEED='0',
               REPLENISHMENT_DATA_DIR=str(output / 'python-settings'),
               LOCALAPPDATA=str(output / 'component-settings'))
    node = ROOT / 'components/aoxiang/runtime/node.exe'
    checks, runs = [], []
    for group, base in [('component_runtime_sha256', ROOT / 'components/aoxiang'),
                        ('input_sha256', baseline_path.parent), ('core_sha256', ROOT),
                        ('protected', Path())]:
        for name, expected in manifest[group].items():
            if sha(base / name) != expected:
                raise ValueError('Protected file changed: ' + name)
        checks.append({'group': group, 'count': len(manifest[group])})
    archive = baseline_path.parent / 'baseline-source.zip'
    if sha(archive) != manifest['source_archive_sha256']:
        raise ValueError('Frozen baseline archive changed')
    original_sources = []
    with zipfile.ZipFile(archive) as zipped:
        for name in zipped.namelist():
            if name.startswith('src/') and not name.endswith('/'):
                if sha(ROOT / name) != hashlib.sha256(zipped.read(name)).hexdigest():
                    raise ValueError('Existing application source changed: ' + name)
                original_sources.append(name)
    component_changes = []
    for name, expected in manifest['component_source_sha256'].items():
        if sha(ROOT / 'components/aoxiang' / name) != expected:
            component_changes.append(name)
    if component_changes != ['src/coordinator.mjs']:
        raise ValueError('Unexpected received-component changes: ' + repr(component_changes))
    commands = [
        ('environment', [sys.executable, 'tools/check_environment.py', '--report', str(output / 'environment.json')], ROOT),
        ('component', [str(node), '--test', 'test/*.test.mjs'], ROOT / 'components/aoxiang'),
        ('python', [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'], ROOT),
    ]
    for name, command, cwd in commands:
        started = time.perf_counter()
        log = output / (name + '.txt')
        with log.open('x', encoding='utf-8', newline='\n') as stream:
            process = subprocess.run(command, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT)
        runs.append({'name': name, 'command': command, 'cwd': str(cwd),
                     'seconds': time.perf_counter() - started, 'exit_code': process.returncode,
                     'log_sha256': sha(log)})
        print(name + ': ' + str(process.returncode), flush=True)
        if process.returncode:
            (output / 'failed-runs.json').write_text(json.dumps(runs, ensure_ascii=False, indent=2), encoding='utf-8', newline='\n')
            raise SystemExit('Required check failed; see ' + str(log))
    py = (output / 'python.txt').read_text(encoding='utf-8')
    js = (output / 'component.txt').read_text(encoding='utf-8')
    py_match, js_match = re.search(r'Ran (\d+) tests in ([\d.]+)s', py), re.search(r'tests (\d+)', js)
    if not py_match or not py.rstrip().endswith('OK') or not js_match or 'fail 0' not in js or 'skipped 0' not in js:
        raise ValueError('Test summary not complete, or tests skipped')
    protected_after = {}
    for name, expected in manifest['protected'].items():
        actual = sha(name)
        if actual != expected:
            raise ValueError('Protected file changed during tests: ' + name)
        protected_after[name] = actual
    report = {
        'task': 'AX02-foundation', 'passed': True, 'baseline_commit': manifest['baseline_commit'],
        'head': git('rev-parse', 'HEAD'), 'branch': git('branch', '--show-current'),
        'working_tree_status': git('status', '--short'), 'checks': checks,
        'original_application_sources_unchanged': original_sources,
        'received_component_changed_files': component_changes,
        'component_change_reason': 'Atomic run planning shared with machine commands, plus exact asset-version audit snapshot. Existing export selectors and validation retained.',
        'component_tests': int(js_match[1]), 'python_tests': int(py_match[1]), 'runs': runs,
        'new_code_sha256': {str(p.relative_to(ROOT)): sha(p) for p in [
            *(ROOT / 'components/aoxiang/src').glob('machine-*.mjs'),
            ROOT / 'components/aoxiang/src/bound-browser.mjs', ROOT / 'components/aoxiang/src/run-plan.mjs',
            ROOT / 'components/aoxiang/src/coordinator.mjs',
            ROOT / 'components/aoxiang/assets/schemas/machine.command.schema.json',
            ROOT / 'src/aoxiang_client.py', ROOT / 'src/aoxiang_jobs.py',
        ]},
        'protected_after': protected_after,
        'limitations': [
            'No live platform operation or EXE acceptance in this foundation check.',
            'Python host adapter is implemented and subprocess-tested; user-facing configuration and workbench wiring remain AX03/AX05/AX13.',
            'Purchase creation and receipt synchronization are not yet implemented.',
            'Durations are test-suite wall times, not product performance benchmarks.',
            'Prior failed test log is retained: AX02 first Python run exposed an unclosed test SQLite connection; corrected test fixture without relaxing expectations.'
        ],
    }
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    print(json.dumps({k: report[k] for k in ['passed', 'task', 'component_tests', 'python_tests']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
