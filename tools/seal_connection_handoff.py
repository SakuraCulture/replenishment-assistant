"""Seal 0.48.1 only after its isolated regression and frozen EXE checks pass."""
import hashlib
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QA = ROOT / 'qa/connection_0481'


def read(path):
    return json.loads(path.read_text('utf-8-sig'))


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', 'utf-8')


def main():
    build = read(ROOT / 'build_0.48.1_rc1/build_info.json')
    assert build['version'] == '0.48.1'
    commit = build['source_commit']
    assert not subprocess.check_output(['git', 'diff', commit, '--', 'src', 'tests', 'components'], cwd=ROOT)
    regression = (QA / 'run1/python-all.log').read_text('utf-8-sig')
    count = re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$', regression)
    assert count and int(count[1]) >= 1227, 'Full regression has not passed'
    focused = (QA / 'run1/python-focused-final.log').read_text('utf-8-sig')
    assert re.search(r'Ran 41 tests .*\s+OK\s*$', focused)
    node = (QA / 'run1/node-all.log').read_text('utf-8-sig')
    assert 'pass 181' in node and 'fail 0' in node
    report = read(QA / 'exe1/report.json')
    assert report['passed'] and report['frozen_exe'] and report['source_commit'] == commit
    assert len(report['checks']) == 25
    assert read(QA / 'source1/report.json')['passed']
    previous = read(ROOT / 'releases/0.48.0/rc2/frozen.json')
    old = read(ROOT / 'releases/0.48.0/rc2/validation.json')
    protected = dict(previous['preserved_deliveries'])
    protected['交付_0.48.0_rc2/补货助手/补货助手.exe'] = old['exe_sha256']
    protected[old['zip']] = old['zip_sha256']
    assert all(sha(ROOT / p) == h for p, h in protected.items())
    assert all(sha(ROOT / p) == h for p, h in previous['core'].items())
    release = ROOT / 'releases/0.48.1/rc1'
    release.mkdir(parents=True, exist_ok=False)
    sources = {'build_info.json': ROOT / 'build_0.48.1_rc1/build_info.json',
               'regression.log': QA / 'run1/python-all.log',
               'focused-python.log': QA / 'run1/python-focused-final.log',
               'node-all.log': QA / 'run1/node-all.log',
               'exe-report.json': QA / 'exe1/report.json',
               'source-ui-report.json': QA / 'source1/report.json'}
    for name, source in sources.items():
        shutil.copyfile(source, release / name)
    (release / 'retained-failures').mkdir()
    shutil.copyfile(QA / 'run1/python-focused.log', release / 'retained-failures/python-module-name-error.log')
    (release / 'screenshots').mkdir()
    for source in (QA / 'exe1').glob('*.png'):
        shutil.copyfile(source, release / 'screenshots' / source.name)
    delivery = ROOT / '交付_0.48.1_rc1/补货助手'
    hashes = {p.relative_to(delivery).as_posix(): sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    target = delivery.parent / '补货助手_0.48.1_rc1_全局连接交接修复.zip'
    with zipfile.ZipFile(target, 'x', zipfile.ZIP_DEFLATED) as archive:
        for name in hashes:
            archive.write(delivery / name, '补货助手/' + name)
    with zipfile.ZipFile(target) as archive:
        assert len(archive.namelist()) == len(hashes)
        for name, digest in hashes.items():
            with archive.open('补货助手/' + name) as stream:
                assert hashlib.file_digest(stream, 'sha256').hexdigest() == digest
    write(release / 'manifest.json', {'source_commit': commit, 'files': hashes})
    write(release / 'frozen.json', {'core': previous['core'], 'preserved_deliveries': protected})
    validation = {'passed': True, 'source_commit': commit, 'python_tests': int(count[1]),
                  'python_seconds': float(count[2]), 'node_tests': 181, 'exe_checks': 25,
                  'exe_sha256': sha(delivery / '补货助手.exe'), 'zip': str(target.relative_to(ROOT)),
                  'zip_sha256': sha(target), 'zip_bytes': target.stat().st_size, 'files': len(hashes),
                  'core_unchanged': len(previous['core']), 'preserved_delivery_files': len(protected),
                  'no_live_platform_calls': True, 'real_edge_permission_prompt_retested': False}
    write(release / 'validation.json', validation)
    print(json.dumps(validation, ensure_ascii=False))


if __name__ == '__main__':
    main()
