"""Seal a connection fix only after its isolated regression and frozen EXE checks pass."""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text('utf-8-sig'))


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', 'utf-8')


def main(version='0.48.1'):
    qa = ROOT / ('qa/connection_' + version.replace('.', '').zfill(4))
    current = version == '0.48.2'
    previous_version = '0.48.1' if current else '0.48.0'
    previous_build = 'rc1' if current else 'rc2'
    python_count, focused_count, node_count, exe_count = (1230, 52, 182, 32) if current else (1227, 41, 181, 25)
    build = read(ROOT / f'build_{version}_rc1/build_info.json')
    assert build['version'] == version
    commit = build['source_commit']
    assert not subprocess.check_output(['git', 'diff', commit, '--', 'src', 'tests', 'components'], cwd=ROOT)
    regression = (qa / 'run1/python-all.log').read_text('utf-8-sig')
    count = re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$', regression)
    assert count and int(count[1]) >= python_count, 'Full regression has not passed'
    focused_path = qa / ('run1/focused.log' if current else 'run1/python-focused-final.log')
    focused = focused_path.read_text('utf-8-sig')
    assert re.search(rf'Ran {focused_count} tests .*\s+OK\s*$', focused)
    node = (qa / 'run1/node-all.log').read_text('utf-8-sig')
    assert f'pass {node_count}' in node and 'fail 0' in node
    report = read(qa / 'exe1/report.json')
    assert report['passed'] and report['frozen_exe'] and report['source_commit'] == commit
    assert len(report['checks']) == exe_count
    assert read(qa / 'source1/report.json')['passed']
    previous = read(ROOT / f'releases/{previous_version}/{previous_build}/frozen.json')
    old = read(ROOT / f'releases/{previous_version}/{previous_build}/validation.json')
    protected = dict(previous['preserved_deliveries'])
    protected[f'交付_{previous_version}_{previous_build}/补货助手/补货助手.exe'] = old['exe_sha256']
    protected[old['zip']] = old['zip_sha256']
    assert all(sha(ROOT / p) == h for p, h in protected.items())
    assert all(sha(ROOT / p) == h for p, h in previous['core'].items())
    release = ROOT / f'releases/{version}/rc1'
    release.mkdir(parents=True, exist_ok=False)
    sources = {'build_info.json': ROOT / f'build_{version}_rc1/build_info.json',
               'regression.log': qa / 'run1/python-all.log',
               'focused-python.log': focused_path,
               'node-all.log': qa / 'run1/node-all.log',
               'exe-report.json': qa / 'exe1/report.json',
               'source-ui-report.json': qa / 'source1/report.json'}
    if current:
        comparison = qa / 'run1/close-entry-comparison.json'
        assert read(comparison)['passed']
        sources['close-entry-comparison.json'] = comparison
    for name, source in sources.items():
        shutil.copyfile(source, release / name)
    if not current:
        (release / 'retained-failures').mkdir()
        shutil.copyfile(qa / 'run1/python-focused.log', release / 'retained-failures/python-module-name-error.log')
    (release / 'screenshots').mkdir()
    for source in (qa / 'exe1').glob('*.png'):
        shutil.copyfile(source, release / 'screenshots' / source.name)
    delivery = ROOT / f'交付_{version}_rc1/补货助手'
    hashes = {p.relative_to(delivery).as_posix(): sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    suffix = '配置窗口关闭与回显修复' if current else '全局连接交接修复'
    target = delivery.parent / f'补货助手_{version}_rc1_{suffix}.zip'
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
                  'python_seconds': float(count[2]), 'node_tests': node_count, 'exe_checks': exe_count,
                  'exe_sha256': sha(delivery / '补货助手.exe'), 'zip': str(target.relative_to(ROOT)),
                  'zip_sha256': sha(target), 'zip_bytes': target.stat().st_size, 'files': len(hashes),
                  'core_unchanged': len(previous['core']), 'preserved_delivery_files': len(protected),
                  'no_live_platform_calls': True, 'real_edge_permission_prompt_retested': False}
    write(release / 'validation.json', validation)
    print(json.dumps(validation, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', choices=['0.48.1', '0.48.2'], default='0.48.1')
    main(parser.parse_args().version)
