"""Seal the measured 0.45.2 candidate without overwriting earlier evidence."""
import hashlib
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QA = ROOT / 'qa/deletion_0452'


def read(path):
    return json.loads(path.read_text('utf-8'))


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    release = ROOT / 'releases/0.45.2/rc1'
    delivery = ROOT / '交付_0.45.2_rc1/补货助手'
    reports = {
        'historical-warm-r2': ('historical-warm.json', 6),
        'historical-cold': ('historical-cold.json', 4),
        'historical-multi': ('historical-multi.json', 4),
        'synthetic': ('synthetic.json', 6),
    }
    for name, (_, count) in reports.items():
        result = read(QA / name / 'report.json')
        if not result['passed'] or result['differences'] or len(result['runs']) != count:
            raise ValueError('Incomplete comparison: ' + name)
    frozen = read(QA / 'frozen.json')
    for path, expected in read(QA / 'environment.json')['measured_runtime_sources'].items():
        if sha(ROOT / path) != expected:
            raise ValueError('Runtime changed since measurement: ' + path)
    for path, expected in frozen['protected'].items():
        if sha(Path(path)) != expected:
            raise ValueError('Protected file changed: ' + path)
    for name, count in [('regression.txt', 1024), ('deletion-tests.txt', 44),
                        ('maintenance-tests.txt', 10)]:
        text = (QA / name).read_text('utf-8-sig')
        match = re.search(r'Ran (\d+) tests in [\d.]+s\s+OK\s*$', text)
        if not match or int(match[1]) != count:
            raise ValueError('Regression incomplete: ' + name)
    build = read(ROOT / 'build_0.45.2_rc1/build_info.json')
    ui = read(QA / 'exe/deletion-report.json')
    if (not ui['passed'] or not ui['frozen_exe'] or ui['process_exit']
            or ui['source_commit'] != build['source_commit']
            or ui['exe_sha256'] != sha(delivery / '补货助手.exe')):
        raise ValueError('EXE acceptance mismatch')
    if subprocess.check_output(['git', 'diff', build['source_commit'], '--', 'src'], cwd=ROOT):
        raise ValueError('Source changed since build')
    release.mkdir(parents=True, exist_ok=False)
    for name, (target, _) in reports.items():
        shutil.copyfile(QA / name / 'report.json', release / target)
    for name in ('frozen.json', 'regression.txt', 'deletion-tests.txt', 'maintenance-tests.txt',
                 'diagnosis.json', 'environment.json', 'interrupted-run.json'):
        shutil.copyfile(QA / name, release / name)
    shutil.copyfile(QA / 'exe/deletion-report.json', release / 'exe-report.json')
    shutil.copyfile(ROOT / 'build_0.45.2_rc1/build_info.json', release / 'build_info.json')
    for path in (QA / 'exe').glob('*.png'):
        shutil.copyfile(path, release / path.name)
    files = {p.relative_to(delivery).as_posix(): sha(p)
             for p in sorted(delivery.rglob('*')) if p.is_file()}
    target = delivery.parent / '补货助手_0.45.2_rc1_删除性能优化.zip'
    with zipfile.ZipFile(target, 'x', zipfile.ZIP_DEFLATED) as archive:
        for name in files:
            archive.write(delivery / name, '补货助手/' + name)
    with zipfile.ZipFile(target) as archive:
        if len(archive.namelist()) != len(files):
            raise ValueError('ZIP file count mismatch')
        for name, expected in files.items():
            if hashlib.sha256(archive.read('补货助手/' + name)).hexdigest() != expected:
                raise ValueError('ZIP content mismatch: ' + name)
    manifest = {'version': '0.45.2', 'build_id': 'rc1',
                'source_commit': build['source_commit'],
                'exe_sha256': sha(delivery / '补货助手.exe'),
                'zip_sha256': sha(target), 'files': files}
    (release / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps({'zip': str(target), 'files': len(files)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
