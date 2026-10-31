"""Seal the 0.17.1 local candidate against its isolated acceptance evidence."""
import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QA = ROOT / 'qa/signed_overwrite_0171'
DIST = ROOT / '交付_0.17.1_rc1'
APP = DIST / '补货助手'
REL = ROOT / 'releases/0.17.1/rc1'


def sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def load(path): return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def save(path, value): Path(path).write_bytes((json.dumps(value, ensure_ascii=False, indent=2)+'\n').encode())
def git(*args): return subprocess.check_output(['git', *args], cwd=ROOT, text=True, encoding='utf-8').strip()


def main():
    commit = git('rev-parse', 'HEAD')
    build = load(APP/'_internal/build_info.json')
    if git('status', '--porcelain') or build['source_commit'] != commit:
        raise ValueError('Require clean build commit')
    baseline = load(QA/'baseline.json')
    exports = load(QA/'exports/report.json')
    exe = load(QA/'exe-rc1/report.json')
    if not exports['passed'] or not exe['passed']:
        raise ValueError('Acceptance failed')
    for r in exe['runs']:
        if r['source_only'] or r['exit_code'] or r['remaining_children'] or r['exe_sha256'] != sha(APP/'补货助手.exe') or r['version']['source_commit'] != commit:
            raise ValueError('Actual EXE evidence mismatch')
    tests = {}
    for name, summary in [('units.txt','units-final-run.txt'), ('optimized-tests.txt','optimized-run.txt')]:
        text = (QA/summary).read_text(encoding='utf-8-sig')
        folder = Path(text.split('本轮记录：')[-1].strip())
        record = folder/'tests.txt'
        value = record.read_text(encoding='utf-8-sig')
        if 'Ran 195 tests' not in value or not value.rstrip().endswith('OK'):
            raise ValueError('Unit evidence failed')
        tests[name] = record
    old = ROOT/'交付_0.17.0_rc3'
    if any(sha(old/n) != digest for n,digest in baseline['old_files'].items()):
        raise ValueError('Old delivery changed')
    if any(sha(ROOT/'src'/n) != digest for n,digest in baseline['core'].items()):
        raise ValueError('Business rules/config changed')
    REL.mkdir(parents=True, exist_ok=False)
    for name, value in [('exports.json',exports), ('actual-exe.json',exe), ('baseline.json',baseline)]: save(REL/name,value)
    for name, path in tests.items(): shutil.copy2(path, REL/name)
    shutil.copy2(QA/'source-ui/report.json',REL/'source-ui.json')
    files = {p.relative_to(APP).as_posix():sha(p) for p in APP.rglob('*') if p.is_file()}
    save(REL/'files.json',files)
    target = DIST/'补货助手_0.17.1_rc1.zip'
    with zipfile.ZipFile(target, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for name in files: z.write(APP/name, '补货助手/'+name)
    with zipfile.ZipFile(target) as z:
        if set(z.namelist()) != {'补货助手/'+n for n in files} or any(hashlib.sha256(z.read('补货助手/'+n)).hexdigest()!=digest for n,digest in files.items()):
            raise ValueError('ZIP content mismatch')
    report = {'passed':True,'baseline_commit':baseline['commit'],'build_commit':commit,
              'branch':git('branch','--show-current'),'version':'0.17.1','model_version':'0.3.3-rules',
              'exe_sha256':sha(APP/'补货助手.exe'),'zip_sha256':sha(target),'file_count':len(files),
              'old_delivery_files_verified':len(baseline['old_files']),'old_delivery_changed':False,
              'core_hashes':baseline['core'],'normal_tests':195,'optimized_tests':195,
              'export_cases':len(exports['cases']),'export_differences':exports['differences'],
              'exe_launches':len(exe['runs']),'no_push_or_merge':True,
              'allowed_changes':['same-name signed Excel replacement','actionable locked-file error',
                                 'tracking export audit logging and duplicate-target protection','overwrite UI text and save-dialog option',
                                 'application version and QA-only repetition'],
              'limits':['Occupied or unwritable files cannot be forcibly replaced; retry after closing.',
                        'Batch replacement remains sequential, not an all-files transaction; completed files remain.',
                        'EXE automated UI acceptance uses isolated small fixtures; not a fresh-machine build validation.']}
    save(REL/'release_report.json',report)
    print(json.dumps(report,ensure_ascii=False))


if __name__ == '__main__': main()
