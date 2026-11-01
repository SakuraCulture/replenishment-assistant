"""Seal 0.22.1 local evidence after tests and packaged Qt acceptance; never publish."""
import argparse,hashlib,json,shutil,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
Q=ROOT/'qa/jst_review_0221';OUT=ROOT/'releases/0.22.1/rc1'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8'))
def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def save(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2),encoding='utf-8')
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--normal-run',required=True);ap.add_argument('--optimized-run',required=True);a=ap.parse_args()
    OUT.mkdir(parents=True,exist_ok=False)
    for name,p in [('original-exports',Q/'original-exports/report.json'),('actual-exe',Q/'exe/report.json'),('actual-exe-auto',Q/'exe-auto/report.json')]:
        r=read(p)
        if not r['passed']:raise AssertionError(p)
        shutil.copy2(p,OUT/(name+'.json'))
    for name,run in [('normal',a.normal_run),('optimized',a.optimized_run)]:
        p=ROOT/'qa/dev/runs'/run/'tests.txt';txt=p.read_text(encoding='utf-8')
        if 'Ran 351 tests' not in txt or not txt.rstrip().endswith('OK'):raise AssertionError(p)
        shutil.copy2(p,OUT/(name+'-tests.txt'));shutil.copy2(p.parent/'environment.json',OUT/(name+'-environment.json'))
    baseline=Q/'baseline/src';current=ROOT/'src'
    names={p.name for base in (baseline,current) for p in base.iterdir() if p.is_file()}
    different={n:{'old':sha(baseline/n) if (baseline/n).exists() else None,'new':sha(current/n) if (current/n).exists() else None} for n in sorted(names) if not (baseline/n).exists() or not (current/n).exists() or sha(baseline/n)!=sha(current/n)}
    if set(different)!={'version.json','jst_ui.py','jst_smoke.py','jst_review.py','jst_review_ui.py'}:raise AssertionError(different)
    save(OUT/'source-differences.json',{'baseline_commit':'5323db2e4961563b858c8169c2c2bc2f3d5f53f1','baseline_archive_sha256':sha(Q/'baseline-source.zip'),'differences':different,'other_source_files_unchanged':len(names)-len(different),'comparison':'bytewise SHA256 for every src file; no business core exceptions'})
    old=read(ROOT/'releases/0.22.0/rc2/files-sha256.json');changed=[]
    for p,digest in old.items():
        if sha(ROOT/'交付_0.22.0_rc2/补货助手'/p)!=digest:changed.append(p)
    oldzip=ROOT/'交付_0.22.0_rc2/补货助手_0.22.0_rc2.zip'
    if sha(oldzip)!='bb0a4cf834162a9c4e2505f0e2709eb0ef472e3b652130537831f89965de4dc3':changed.append(str(oldzip))
    save(OUT/'protection.json',{'old_release_files':len(old),'changed':changed,'daily_database_used':False,'qa_data':'isolated synthetic fixtures; no daily database comparison claimed'})
    if changed:raise AssertionError(changed)
    build=ROOT/'build_0.22.1_rc1';info=read(build/'build_info.json')
    for p in current.iterdir():
        if p.is_file() and sha(p)!=sha(build/'source/src'/p.name):raise AssertionError('Built source differs '+p.name)
    for name in ('build_info.json','command.json','build.log'):shutil.copy2(build/name,OUT/name)
    package=ROOT/'交付_0.22.1_rc1/补货助手';exe=package/'补货助手.exe'
    save(OUT/'files-sha256.json',{str(p.relative_to(package)).replace('\\','/'):sha(p) for p in sorted(package.rglob('*')) if p.is_file()})
    save(OUT/'source-hashes.json',{p.name:sha(p) for p in sorted(current.iterdir()) if p.is_file()})
    auto=read(Q/'exe-auto/report.json');manual=read(Q/'exe/report.json')
    for r in (auto,manual):
        if not r['actual_exe'] or r['exe_sha256']!=sha(exe):raise AssertionError('Packaged executable mismatch')
    reports=[auto,*manual['runs']]
    for r in reports:
        if r['exit_code'] or r['remaining_children']:raise AssertionError('Packaged acceptance mismatch')
    archive=package.parent/'补货助手_0.22.1_rc1.zip'
    if archive.exists():raise FileExistsError(archive)
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for p in sorted(package.rglob('*')):
            if p.is_file():z.write(p,str(p.relative_to(package.parent)))
    save(OUT/'release.json',{'passed':True,'build':info,'exe_sha256':sha(exe),'zip_sha256':sha(archive),'tests_per_mode':351,'actual_exe_starts':len(reports),'pushed':False,'merged':False})
    print(json.dumps({'release':str(OUT),'exe':str(exe),'zip':str(archive)},ensure_ascii=False))
if __name__=='__main__':main()
