"""Verify/seal the 0.20.0 candidate without touching daily data or old packages.

Default is read-only verification; --seal creates a new evidence directory/ZIP.
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QA = ROOT/'qa/final_review_0200'
DIST = ROOT/'交付_0.20.0_rc1'
APP = DIST/'补货助手'
REL = ROOT/'releases/0.20.0/rc1'
ZIP = DIST/'补货助手_0.20.0_rc1.zip'


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save(path, value):
    Path(path).write_bytes((json.dumps(value, ensure_ascii=False, indent=2)+'\n').encode('utf-8'))


def evidence_copy(source, target):
    # Git evidence uses LF; preserve the original QA bytes and all values/text.
    if Path(target).suffix in ('.json', '.txt', '.md'):
        Path(target).write_bytes(Path(source).read_bytes().replace(b'\r\n', b'\n'))
    else:
        shutil.copy2(source, target)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True, encoding='utf-8').strip()


def verify():
    build = read(APP/'_internal/build_info.json')
    commit = build['source_commit']
    require(build['version'] == '0.20.0', 'Wrong candidate')
    require(not git('diff', commit, '--', 'src', 'prepare_delivery.py', 'build.ps1',
                    'requirements-win-amd64.lock'), 'Runtime differs from actual EXE build')
    baseline = read(QA/'baseline.json')
    core = {name:sha(ROOT/'src'/name) for name in baseline['core']}
    require(core == baseline['core'], 'Protected business implementation changed')
    preserved = {**baseline['protected'], **baseline['inputs']}
    changed = [p for p, digest in preserved.items() if not Path(p).is_file() or sha(p) != digest]
    require(not changed, 'Protected files changed: '+repr(changed))
    capture = read(QA/'model_final/capture.json')
    require(all(sha(ROOT/name) == digest for name, digest in capture['source_hashes'].items()),
            'Final business capture source differs')
    eq = read(QA/'equivalence-final.json')
    require(eq['passed'] and len(eq['cases']) == 30 and not any(c['differences'] for c in eq['cases']),
            'Strict business comparison failed')
    archives = read(QA/'archive-equivalence/report.json')
    require(archives['passed'] and len(archives['cases']) == 12 and archives['database_unchanged'],
            'Archive equivalence failed')
    exe = read(QA/'actual-exe/report.json')
    require(exe['passed'] and len(exe['runs']) == 2, 'EXE acceptance failed')
    for run in exe['runs']:
        require(run['passed'] and not run['source_only'] and run['exit_code'] == 0 and
                not run['remaining_children'] and run['exe_sha256'] == sha(APP/'补货助手.exe') and
                run['version']['source_commit'] == commit, 'EXE evidence does not bind this candidate')
    editor = read(QA/'actual-exe/final-review.json')
    require(editor['passed'] and editor['native_quantity_edits'] == 30 and
            all(editor[k] for k in ('money_matches_core', 'stale_risks_cleared',
                'bulk_scope_isolated', 'read_only_excluded', 'combined_filters', 'host_results_unchanged')),
            'Actual editor regression failed')
    tests = {}
    for name in ('units', 'optimized'):
        summary = (QA/(name+'-release.txt')).read_text(encoding='utf-8-sig')
        directory = Path(summary.split('本轮记录：')[-1].strip())
        contents = (directory/'tests.txt').read_text(encoding='utf-8-sig')
        require('Ran 304 tests' in contents and contents.rstrip().endswith('OK'), 'Unit tests failed')
        require(read(directory/'environment.json')['passed'], 'Dependency check failed')
        tests[name] = directory
    performance = read(QA/'performance-final/report.json')
    require(all(len(performance['runs'][mode]) == 3 for mode in ('old', 'new')), 'Missing benchmark rounds')
    files = {p.relative_to(APP).as_posix():sha(p) for p in sorted(APP.rglob('*')) if p.is_file()}
    clean_app = QA/'build_checkout/交付_0.20.0_rc1/补货助手'
    require(files == {p.relative_to(clean_app).as_posix():sha(p) for p in clean_app.rglob('*') if p.is_file()},
            'Delivered files differ from clean checkout build')
    report = {'passed':True, 'version':'0.20.0', 'model_version':'0.3.3-rules',
              'routing_version':'0.5.1', 'storage_format_version':2,
              'baseline_commit':baseline['commit'], 'build_commit':commit,
              'branch':git('branch', '--show-current'), 'core_hashes':core,
              'protected_file_count':len(preserved), 'input_count':len(baseline['inputs']),
              'normal_tests':304, 'optimized_tests':304, 'strict_cases':30, 'strict_differences':0,
              'archive_cases':12, 'archive_differences':0, 'exe_launches':2,
              'native_quantity_edits':30, 'exe_sha256':sha(APP/'补货助手.exe'), 'file_count':len(files),
              'clean_build_copy_verified':True, 'no_push_or_merge':True,
              'limits':['Original native crash not deterministically reproduced.',
                        'Benchmark: synthetic 14244 rows; end RSS is not peak memory.',
                        'EXE exercise timer gap includes intentionally suspended QA driver during editor exercise; not pure UI latency.',
                        'No cross-machine acceptance claim; all fixtures/databases are isolated.']}
    return report, files, tests


def verify_zip(files):
    with zipfile.ZipFile(ZIP) as archive:
        require(set(archive.namelist()) == {'补货助手/'+name for name in files}, 'ZIP file set mismatch')
        require(archive.testzip() is None, 'ZIP CRC failure')
        require(all(hashlib.sha256(archive.read('补货助手/'+name)).hexdigest() == digest
                    for name, digest in files.items()), 'ZIP file content mismatch')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seal', action='store_true')
    args = parser.parse_args()
    report, files, tests = verify()
    if args.seal:
        require(not REL.exists() and not ZIP.exists(), 'Evidence/ZIP exists; use default read-only verification')
        REL.mkdir(parents=True)
        evidence = {
            'baseline.json':'baseline.json', 'equivalence.json':'first-equivalence.json',
            'equivalence-final.json':'equivalence.json', 'model_final/capture.json':'final-capture.json',
            'archive-equivalence/report.json':'archive_exports.json',
            'performance-final/report.json':'performance.json', 'actual-exe/report.json':'exe.json',
            'actual-exe/final-review.json':'editor.json', 'source-native-final/report.json':'source-native.json',
            'build.txt':'build.txt', 'actual-exe/final-live-money.png':'final-live-money.png',
            'actual-exe/combined-filters.png':'combined-filters.png',
            'actual-exe/read-only-excluded.png':'read-only-excluded.png',
            'actual-exe/money-manual-applied-overview.png':'overview.png'}
        for source, target in evidence.items():
            evidence_copy(QA/source, REL/target)
        for name, directory in tests.items():
            evidence_copy(directory/'tests.txt', REL/(name+'.txt'))
            evidence_copy(directory/'environment.json', REL/(name+'-environment.json'))
        save(REL/'files.json', files)
        with zipfile.ZipFile(ZIP, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name in files:
                archive.write(APP/name, '补货助手/'+name)
        verify_zip(files)
        report.update(zip_sha256=sha(ZIP), zip_bytes=ZIP.stat().st_size)
        save(REL/'manifest.json', report)
    elif REL.exists():
        require(files == read(REL/'files.json'), 'Sealed files changed')
        verify_zip(files)
        require(sha(ZIP) == read(REL/'manifest.json')['zip_sha256'], 'Sealed ZIP changed')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
