"""Seal independently built 0.35.1 evidence; rejects unverified or changed binaries."""
import argparse
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    with path.open('rb') as stream: return hashlib.file_digest(stream, 'sha256').hexdigest()


def read(path): return json.loads(path.read_text('utf-8'))
def save(path, data): path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')


def copy_evidence(origin, target):
    if origin.suffix.lower() in {'.json', '.txt', '.log'}:
        target.write_bytes(origin.read_bytes().replace(b'\r\n', b'\n'))
    else: shutil.copy2(origin, target)


def main():
    parser = argparse.ArgumentParser()
    for key in ('qa', 'ui', 'delivery', 'out'): parser.add_argument('--' + key, type=Path, required=True)
    parser.add_argument('--candidate', default='after')
    args = parser.parse_args(); qa, ui, delivery, out = (getattr(args, k).resolve() for k in ('qa', 'ui', 'delivery', 'out'))
    if out.exists(): raise FileExistsError('Release evidence directory must be new')
    build = read(delivery / '_internal/build_info.json')
    equivalence = read(qa / 'equivalence.json'); exe = read(ui / 'automation-report.json'); recipes = read(ui / 'playbook-report.json')
    client = read(ui / 'codex-client/report.json'); evidence = read(ui / 'delivery-evidence.json')
    if not (equivalence['passed'] and exe['passed'] and exe['frozen_exe'] and recipes['passed'] and client['passed'] and len(recipes['cases']) == 9):
        raise ValueError('Required acceptance has not passed')
    if build['source_commit'] != evidence['build']['source_commit']: raise ValueError('EXE build differs from acceptance')
    from structure_oracle import source_hashes
    source = ROOT / ('build_' + build['version'] + '_' + build['build_id']) / 'source'
    if source_hashes(source) != source_hashes(ROOT): raise ValueError('Current sources differ from built sources')
    capture = read(qa / args.candidate / 'capture.json')
    if capture['PYTHONHASHSEED'] != read(qa / 'before/capture.json')['PYTHONHASHSEED']:
        raise ValueError('Comparison processes must use the same hash seed')
    if capture['source_hashes'] != source_hashes(ROOT): raise ValueError('Comparison is for different source files')
    if any(sha(Path(p)) != digest for p, digest in evidence['hashes'].items()): raise ValueError('Verified executable changed')
    baseline = read(qa / 'baseline.json')
    changed = [p for p, digest in baseline['protected'].items() if not Path(p).is_file() or sha(Path(p)) != digest]
    if changed: raise ValueError('Protected files changed: ' + str(changed))
    if 'Ran 741 tests' not in (qa / 'full-tests.log').read_text('utf-8') or not (qa / 'full-tests.log').read_text('utf-8').rstrip().endswith('OK'):
        raise ValueError('Missing full regression pass')
    out.mkdir(parents=True)
    for origin, name in [(qa/'equivalence.json', 'equivalence.json'), (qa/'full-tests.log', 'full-tests.txt'),
                         (qa/'automation-final.log', 'automation-tests.txt'), (ui/'automation-report.json', 'exe-report.json'),
                         (ui/'playbook-report.json', 'business-cases.json'), (ui/'codex-client/report.json', 'codex-client.json'),
                         (ui/'delivery-evidence.json', 'build-evidence.json'), (ui/'AI常用业务.png', 'AI常用业务.png')]:
        copy_evidence(origin, out/name)
    for prompt in ui.glob('prompt-*.txt'): copy_evidence(prompt, out/prompt.name)
    manifest = {str(p.relative_to(delivery)): sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    save(out/'files.json', manifest)
    archive = delivery.parent / ('补货助手_' + build['version'] + '_' + build['build_id'] + '_AI常用业务.zip')
    if archive.exists(): raise FileExistsError('Archive already exists')
    with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path in manifest: z.write(delivery/path, '补货助手/' + path.replace('\\', '/'))
    with zipfile.ZipFile(archive) as z:
        if z.testzip(): raise ValueError('ZIP CRC mismatch')
        for path, digest in manifest.items():
            if hashlib.sha256(z.read('补货助手/' + path.replace('\\', '/'))).hexdigest() != digest: raise ValueError('ZIP content mismatch')
    result = dict(version=build['version'], source_commit=build['source_commit'], model='0.3.4-rules',
        build_id=build['build_id'], exe_sha256=sha(delivery/'补货助手.exe'),
        bridge_sha256=sha(delivery/'AI接口/补货助手AI接口.exe'), zip=str(archive), zip_sha256=sha(archive),
        file_count=len(manifest), protected_count=len(baseline['protected']), protected_unchanged=True,
        business_cases=9, natural_language_model_runs=0, full_tests=741, differences=equivalence['difference_count'])
    save(out/'package.json', result); print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__': main()
