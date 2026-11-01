"""Verify the received component copy, frozen inputs, protected files and baseline runs."""
import argparse
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qa', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    q = args.qa.resolve()
    manifest = json.loads((q / 'baseline.json').read_text(encoding='utf-8'))
    checked = []
    for group, base in [('component_source_sha256', ROOT / 'components/aoxiang'),
                        ('component_runtime_sha256', ROOT / 'components/aoxiang'),
                        ('input_sha256', q), ('core_sha256', ROOT), ('protected', Path())]:
        for name, expected in manifest[group].items():
            if sha(base / name) != expected:
                raise ValueError('Changed protected input: ' + name)
        checked.append({'group': group, 'files': len(manifest[group])})
    if sha(q / 'baseline-source.zip') != manifest['source_archive_sha256']:
        raise ValueError('Baseline source archive changed')
    component_log = (q / 'baseline-component-tests.txt').read_text(encoding='utf-8-sig')
    python_log = (q / 'baseline-python-tests.txt').read_text(encoding='utf-8-sig')
    py = re.search(r'Ran (\d+) tests in ([\d.]+)s', python_log)
    node = re.search(r'tests (\d+)', component_log)
    if not py or not python_log.rstrip().endswith('OK') or not node or 'fail 0' not in component_log:
        raise ValueError('Baseline tests did not all pass')
    capture = json.loads((q / 'baseline-capture/capture.json').read_text(encoding='utf-8'))
    expected_real = {f'real_{limit}_{flag}' for limit, flag in
                     [(None, True), (0, True), (3000, True), (5000, True), (10000, True), (None, False)]}
    if not expected_real.issubset(capture['cases']) or 'real_custom' not in capture['cases']:
        raise ValueError('Baseline capture lacks required real-data parameter variants')
    for name in capture['cases']:
        if not (q / 'baseline-capture' / (name + '.pklz')).is_file():
            raise ValueError('Missing captured case: ' + name)
    report = {
        'passed': True, 'task': 'AX01', 'baseline_commit': manifest['baseline_commit'],
        'checks': checked,
        'python_tests': {'passed': int(py[1]), 'seconds': float(py[2])},
        'component_tests': {'passed': int(node[1])},
        'capture_cases': capture['cases'],
        'capture_files': {p.name: sha(p) for p in (q / 'baseline-capture').iterdir() if p.is_file()},
        'logs': {n: sha(q / n) for n in ['baseline-component-tests.txt', 'baseline-python-tests.txt', 'baseline-capture.txt']},
        'qa_path': str(q),
        'scope': 'Baseline freezing and existing isolated tests only; no candidate business equivalence or live-platform/EXE acceptance yet.'
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open('x', encoding='utf-8', newline='\n') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write('\n')
    print(json.dumps({k: report[k] for k in ['passed', 'task', 'python_tests', 'component_tests']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
