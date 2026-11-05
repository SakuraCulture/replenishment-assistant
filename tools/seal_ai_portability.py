"""Seal 0.40.0 local candidate after EXE and real-client evidence review.

No publication, no overwrite, and no recursive copying of private QA settings.
"""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def read(path):
    return json.loads(Path(path).read_text('utf-8'))


def require(value, message):
    if not value:
        raise ValueError(message)


def tests(path):
    raw = Path(path).read_bytes()
    text = raw.decode('utf-16') if raw.startswith(b'\xff\xfe') else raw.decode('utf-8')
    match = re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$', text)
    require(match, 'Incomplete unittest evidence: ' + str(path))
    return {'count': int(match[1]), 'seconds': float(match[2]), 'passed': True}, text


def main():
    p = argparse.ArgumentParser()
    for key in ('qa', 'delivery', 'release', 'zip'):
        p.add_argument('--' + key, type=Path, required=True)
    a = p.parse_args()
    qa, delivery, release, archive = (getattr(a, k).resolve() for k in ('qa', 'delivery', 'release', 'zip'))
    require(not release.exists() and not archive.exists(), 'Use new evidence and ZIP paths')
    baseline = read(qa / 'baseline.json')
    build = read(delivery / '_internal/build_info.json')
    require(build['version'] == '0.40.0', 'Wrong application version')
    proof = read(qa / 'exe-rc2/report.json')
    require(proof['passed'] and proof['build'] == build, 'Wrong or failed EXE proof')
    for path, digest in proof['hashes'].items():
        require(sha(path) == digest, 'Verified executable changed: ' + path)
    for value in proof['checks'].values():
        require(value['passed'] and value['frozen_exe'], 'Actual executable verification required')
    strict = read(qa / 'equivalence-final/report.json')
    require(strict['passed'] and strict['baseline'] == baseline['commit'], 'Baseline mismatch')
    require(strict['calculation']['difference_count'] == strict['history_difference_count'] == 0, 'Business differences')
    require(not strict['excel']['value_type_order_style_differences'], 'Excel differences')
    ai = read(qa / 'real-ai-review.json')
    require(ai['review_complete'], 'Actual client findings must be reviewed')
    artifacts = read(qa / 'real-ai-artifacts/report.json')
    require(artifacts['passed'] and artifacts['formal_unchanged'] and artifacts['current_unchanged'], 'AI changed business state')
    require(read(qa / 'real-ai-artifacts/contract.json')['passed'], 'AI evidence contract validation failed')
    restoration = read(qa / 'workbuddy-restoration.json')
    require(restoration['temporary_entry_removed'] and restoration['unrelated_unchanged'], 'Client cleanup incomplete')
    protected = []
    for path, digest in baseline['files'].items():
        frozen = subprocess.check_output(['git', 'show', build['source_commit'] + ':' + path], cwd=ROOT)
        require(sha(ROOT / path) == digest == hashlib.sha256(frozen).hexdigest(), 'Protected source changed: ' + path)
        protected.append({'path': path, 'sha256': digest, 'unchanged': True})
    for path, digest in baseline['old_package'].items():
        require(sha(ROOT / path) == digest, 'Old delivery changed: ' + path)
    require(not subprocess.check_output(['git', 'diff', build['source_commit'], '--', 'src', 'tests'], cwd=ROOT), 'Runtime/test edits after build')
    full, full_log = tests(qa / 'all-tests-final.log')
    targeted, targeted_log = tests(qa / 'targeted-final.log')
    release.mkdir(parents=True)
    selected = {
        'baseline.json': 'baseline.json',
        'equivalence-final/report.json': '严格等价对照.json',
        'exe-rc2/report.json': '实际EXE验收.json',
        'source-ui/portability-exe.json': '源码连接流程.json',
        'exe-rc2/portability/codex-configuration.png': 'Codex连接配置.png',
        'exe-rc2/portability/workbuddy-configuration.png': 'WorkBuddy连接配置.png',
        'codex/report.json': 'Codex实际执行.json',
        'real-ai-review.json': '真实AI判定.json',
        'real-ai-artifacts/report.json': '真实AI产物核验.json',
        'real-ai-artifacts/contract.json': '真实AI新增依据核验.json',
        'workbuddy-restoration.json': '客户端恢复核验.json',
    }
    for source, name in selected.items():
        shutil.copy2(qa / source, release / name)
    (release / '全量回归.log.txt').write_bytes(full_log.encode('utf-8'))
    (release / '最终定向回归.log.txt').write_bytes(targeted_log.encode('utf-8'))
    # Explicit allowlist only: excludes connection secrets, private original client
    # config, browser state, arbitrary QA DBs and personal client records.
    allowed = [qa / 'real-ai/tool-calls.jsonl', qa / 'real-ai/final.json', qa / 'real-ai/oracle.json']
    for case in ('funnel', 'history', 'diagnosis'):
        for name in ('prompt.txt', 'answer.txt', 'events.jsonl', 'stderr.txt'):
            allowed.append(qa / 'codex' / case / name)
    for name in ('prompt.txt', 'answer.txt', 'result.json'):
        candidate = qa / 'workbuddy' / name
        if candidate.exists():
            allowed.append(candidate)
    for artifact in artifacts['artifacts']:
        path = Path(artifact['path']).resolve()
        require(path.is_relative_to(qa / 'real-ai/exports'), 'Unexpected export scope')
        allowed.append(path)
    evidence_manifest = [{'path': f.relative_to(qa).as_posix(), 'bytes': f.stat().st_size, 'sha256': sha(f)} for f in allowed]
    with zipfile.ZipFile(release / '合成AI验收证据.zip', 'x', zipfile.ZIP_DEFLATED) as z:
        for f in allowed:
            z.write(f, f.relative_to(qa).as_posix())
    (release / 'AI证据清单.json').write_text(json.dumps(evidence_manifest, ensure_ascii=False, indent=2), 'utf-8')
    manifest = [{'path': f.relative_to(delivery).as_posix(), 'bytes': f.stat().st_size, 'sha256': sha(f)}
                for f in sorted(delivery.rglob('*')) if f.is_file()]
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for item in manifest:
            z.write(delivery / item['path'], delivery.name + '/' + item['path'])
    with zipfile.ZipFile(archive) as z:
        require(z.testzip() is None, 'ZIP CRC failure')
        for item in manifest:
            require(hashlib.sha256(z.read(delivery.name + '/' + item['path'])).hexdigest() == item['sha256'], 'ZIP content mismatch')
    (release / '交付文件清单.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), 'utf-8')
    result = {
        'version': '0.40.0', 'build': build, 'baseline_commit': baseline['commit'],
        'branch': subprocess.check_output(['git', 'branch', '--show-current'], cwd=ROOT, text=True).strip(),
        'full_tests': {**full, 'commit': 'f52f756725e3e84d7c10ed59f9c3e04f23cca556'},
        'final_targeted_tests': {**targeted, 'commit': build['source_commit']},
        'protected_files': protected, 'old_delivery_unchanged': True,
        'old_delivery_checked': baseline['old_package'],
        'actual_exe_passed': True, 'business_difference_count': 0,
        'exe_hashes': proof['hashes'], 'zip': str(archive), 'zip_sha256': sha(archive),
        'zip_bytes': archive.stat().st_size, 'delivery_files': len(manifest),
        'environment': {'python': sys.version, 'platform': platform.platform(),
            'packages': {x: importlib.metadata.version(x) for x in ('PySide6-Essentials', 'openpyxl', 'PyInstaller')}},
        'ai_review': ai, 'client_restoration': restoration,
        'limits': ['全部新增数据为明确标注的合成验收数据', '未在第二台物理电脑验证',
                   '自然语言通过仅覆盖本轮问题，不等于任意提示词可靠',
                   '真实AI使用源码工作台加包内接口；完整EXE另做原生及stdio验收',
                   '未重跑平台取数及整夜定时链路', '未推送、未合并'],
    }
    (release / '交付核验.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
    # Repository evidence uses LF. Original logs and AI artifacts remain in QA
    # and the explicit evidence archive; only whitespace of text copies changes.
    for path in release.iterdir():
        if path.suffix in ('.json', '.txt', '.md'):
            text = path.read_text('utf-8')
            path.write_bytes(('\n'.join(line.rstrip() for line in text.splitlines()).rstrip() + '\n').encode('utf-8'))
    print(json.dumps({'release': str(release), 'zip': str(archive), 'zip_sha256': result['zip_sha256'],
                      'delivery_files': len(manifest)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
