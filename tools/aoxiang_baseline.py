"""Freeze the received component and current Git source without touching user state.

The original package is read-only. Vendoring is allowlisted; executables and npm
dependencies are installed locally from that exact verified package, never tracked.
The QA directory and component destination must both be new.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
CORE = ('model.py', 'one_box_exception.py', 'allocation.py', 'default_config.json',
        'default_must_sell.json', 'warehouse_defaults.py')
PACKAGE_HASH = 'd2061a906133cd788154e6ab0c0ff4414d9faa93172daf546624b1c62b8c43d1'
ROOT_FILES = ('package.json', 'package-lock.json', 'NOTICE.txt', 'README.md',
              '实现与验证.md', '验收记录.json', '启动.cmd', '.dev-checkout')
BUILD_SCRIPTS = ('build-release.mjs', 'prepare-runtime.mjs')


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def archive_member(root, name):
    parts = PurePosixPath(name)
    if parts.is_absolute() or '..' in parts.parts or '\\' in name or ':' in name:
        raise ValueError('Unsafe archive member: ' + name)
    target = (root / Path(*parts.parts)).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError('Archive escapes output directory')
    return target


def freeze(args):
    output = args.output.resolve()
    component = ROOT / 'components/aoxiang'
    dev = args.component_root.resolve()
    package = args.component_zip.resolve()
    if output.exists() or component.exists():
        raise ValueError('Output/component already exists; refusing to overwrite')
    if sha(package) != PACKAGE_HASH:
        raise ValueError('Received 0.2.1 ZIP hash differs from reviewed package')
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    selected = [dev / n for n in ROOT_FILES]
    for folder in ('src', 'assets', 'test'):
        selected.extend(p for p in (dev / folder).rglob('*') if p.is_file())
    selected.extend(dev / 'scripts' / n for n in BUILD_SCRIPTS)
    for p in selected:
        if not p.is_file() or p.is_symlink():
            raise ValueError('Missing or linked component input: ' + str(p))
    with zipfile.ZipFile(package) as zipped:
        roots = {PurePosixPath(n).parts[0] for n in zipped.namelist()}
        if len(roots) != 1:
            raise ValueError('Expected one release directory in received package')
        prefix = next(iter(roots)) + '/'
        manifest = json.loads(zipped.read(prefix + 'integrity.manifest.json'))
        by_name = {f['relativePath']: f for f in manifest['files']}
        if len(by_name) != len(manifest['files']):
            raise ValueError('Duplicate package manifest entries')
        for name, info in by_name.items():
            archive_member(component, name)
            if hashlib.sha256(zipped.read(prefix + name)).hexdigest() != info['sha256']:
                raise ValueError('Package integrity mismatch: ' + name)
        for p in selected:
            relative = p.relative_to(dev).as_posix()
            if relative in by_name and sha(p) != by_name[relative]['sha256']:
                raise ValueError('Development source differs from received ZIP: ' + relative)
            if relative.startswith(('src/', 'assets/')) and relative not in by_name:
                raise ValueError('Unreviewed development source: ' + relative)
        # All checks precede writes. Failures preserve any partial output for diagnosis.
        output.mkdir(parents=True)
        component.mkdir(parents=True)
        for p in selected:
            target = archive_member(component, p.relative_to(dev).as_posix())
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
        for name in by_name:
            if name.startswith(('runtime/', 'node_modules/')):
                target = archive_member(component, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('xb') as f:
                    f.write(zipped.read(prefix + name))
    archive = output / 'baseline-source.zip'
    subprocess.run(['git', 'archive', '--format=zip', '-o', str(archive), commit], cwd=ROOT, check=True)
    source = output / 'baseline'
    source.mkdir()
    with zipfile.ZipFile(archive) as zipped:
        for info in zipped.infolist():
            target = archive_member(source, info.filename)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('xb') as f:
                    f.write(zipped.read(info))
    inputs = output / 'inputs'
    shutil.copytree(args.fixtures, inputs)
    user_inputs = output / 'provided-inputs'
    user_inputs.mkdir()
    provided = {}
    for filename in ('门店数据导出 (4).xlsx', '采购单批量导入服务 (2).xlsx', '采购子单详情 (5).xlsx'):
        original = args.downloads / filename
        target = user_inputs / filename
        before = sha(original)
        shutil.copy2(original, target)
        if sha(target) != before or sha(original) != before:
            raise ValueError('Input changed while freezing: ' + filename)
        provided[str(original)] = before
    protected = {str(package): PACKAGE_HASH, **provided}
    for filename in ('补货助手_0.23.0_rc1.zip', '补货助手/补货助手.exe'):
        p = ROOT / '交付_0.23.0_rc1' / filename
        if p.exists():
            protected[str(p)] = sha(p)
    record = {
        'baseline_commit': commit, 'branch': subprocess.check_output(
            ['git', 'branch', '--show-current'], cwd=ROOT, text=True).strip(),
        'scope': 'Aoxiang only; JST manual; upload creates draft; no auto submit/inbound',
        'component_version': '0.2.1', 'received_package_sha256': PACKAGE_HASH,
        'component_source_sha256': {p.relative_to(dev).as_posix(): sha(p) for p in selected},
        'component_runtime_sha256': {n: v['sha256'] for n, v in by_name.items()
                                    if n.startswith(('runtime/', 'node_modules/'))},
        'core_sha256': {f'src/{n}': sha(ROOT / 'src' / n) for n in CORE if (ROOT / 'src' / n).exists()},
        'input_sha256': {p.relative_to(output).as_posix(): sha(p)
                        for folder in (inputs, user_inputs) for p in folder.rglob('*') if p.is_file()},
        'protected': protected, 'source_archive_sha256': sha(archive),
        'baseline_tests_executed': False,
        'limits': 'Hashes prove freezing only; test results are recorded separately. User databases are not opened.'
    }
    write_json(output / 'baseline.json', record)
    write_json(ROOT / 'releases/aoxiang-integration/baseline.json', record)
    print(json.dumps({'baseline_commit': commit, 'component_files': len(selected),
                      'fixtures': len(record['input_sha256']), 'output': str(output)}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--component-root', type=Path, required=True)
    parser.add_argument('--component-zip', type=Path, required=True)
    parser.add_argument('--fixtures', type=Path, required=True)
    parser.add_argument('--downloads', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    freeze(parser.parse_args())


if __name__ == '__main__':
    main()
