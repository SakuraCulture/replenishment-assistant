"""Seal 0.23.0 evidence; reject stale reports/builds and preserve old packages."""
import argparse,hashlib,json,shutil,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
Q=ROOT/'qa/jst_results_0230';OUT=ROOT/'releases/0.23.0/rc1'
BASELINE='63a1b18ee22693c21cbf1024198e43025df652e3'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8'))
def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def save(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--normal-run',required=True);ap.add_argument('--optimized-run',required=True);a=ap.parse_args()
    OUT.mkdir(parents=True,exist_ok=False)
    reports={}
    for name,folder in [('ordered-jst-exports','compare'),('original-exports','original-exports'),('actual-exe','exe-rc1'),('actual-exe-original','exe-original-rc1'),('actual-exe-auto','exe-auto-rc1')]:
        r=read(Q/folder/'report.json')
        if not r['passed']:raise AssertionError(folder)
        save(OUT/(name+'.json'),r);reports[name]=r
    save(OUT/'frozen-inputs.json',read(Q/'compare/inputs.json'))
    for name,run in [('normal',a.normal_run),('optimized',a.optimized_run)]:
        p=ROOT/'qa/dev/runs'/run/'tests.txt';txt=p.read_text(encoding='utf-8')
        if 'Ran 363 tests' not in txt or not txt.rstrip().endswith('OK'):raise AssertionError(p)
        shutil.copy2(p,OUT/(name+'-tests.txt'));save(OUT/(name+'-environment.json'),read(p.parent/'environment.json'))
    base=Q/'baseline/src';current=ROOT/'src';names={p.name for d in (base,current) for p in d.iterdir() if p.is_file()}
    changes={n:{'old':sha(base/n) if (base/n).exists() else None,'new':sha(current/n) if (current/n).exists() else None} for n in sorted(names) if not (base/n).exists() or not (current/n).exists() or sha(base/n)!=sha(current/n)}
    allowed={'version.json','app.py','history_read.py','jst_smoke.py','jst_task_ui.py','jst_ui.py','lifecycle_home_ui.py','jst_results.py','jst_results_ui.py','jst_results_smoke.py'}
    if set(changes)!=allowed:raise AssertionError(changes)
    save(OUT/'source-differences.json',{'baseline_commit':BASELINE,'baseline_archive_sha256':sha(Q/'baseline-source.zip'),'differences':changes,'other_source_files_unchanged':len(names)-len(changes),'scope':'every src file bytewise SHA256; no business core exclusions'})
    old=read(ROOT/'releases/0.22.1/rc2/files-sha256.json');changed=[]
    for p,digest in old.items():
        if sha(ROOT/'交付_0.22.1_rc2/补货助手'/p)!=digest:changed.append(p)
    if sha(ROOT/'交付_0.22.1_rc2/补货助手_0.22.1_rc2.zip')!='1db35cd2bd5f3dd272f745239100bfcfd941b302f064d9a6a21454ebae5cbd36':changed.append('old ZIP')
    save(OUT/'protection.json',{'old_release_files':len(old),'changed':changed,'daily_database_used':False,'scope':'isolated synthetic fixtures; no daily database static hash comparison claimed'})
    if changed:raise AssertionError(changed)
    build=ROOT/'build_0.23.0_rc1';info=read(build/'build_info.json')
    for p in current.iterdir():
        if p.is_file() and sha(p)!=sha(build/'source/src'/p.name):raise AssertionError('Built source differs '+p.name)
    for name in ('build_info.json','command.json','build.log'):shutil.copy2(build/name,OUT/name)
    package=ROOT/'交付_0.23.0_rc1/补货助手';exe=package/'补货助手.exe';starts=[]
    for name in ('actual-exe','actual-exe-original','actual-exe-auto'):
        r=reports[name]
        if not r['actual_exe'] or r['exe_sha256']!=sha(exe):raise AssertionError(name)
        starts.extend(r.get('runs',[r]))
    for r in starts:
        if r['exit_code'] or r['remaining_children'] or r.get('source_commit',info['source_commit'])!=info['source_commit']:raise AssertionError(r)
    save(OUT/'files-sha256.json',{str(p.relative_to(package)).replace('\\','/'):sha(p) for p in sorted(package.rglob('*')) if p.is_file()})
    save(OUT/'source-hashes.json',{p.name:sha(p) for p in sorted(current.iterdir()) if p.is_file()})
    archive=package.parent/'补货助手_0.23.0_rc1.zip'
    if archive.exists():raise FileExistsError(archive)
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for p in sorted(package.rglob('*')):
            if p.is_file():z.write(p,str(p.relative_to(package.parent)))
    save(OUT/'release.json',{'passed':True,'build':info,'exe_sha256':sha(exe),'zip_sha256':sha(archive),'tests_per_mode':363,'actual_exe_starts':len(starts),'pushed':False,'merged':False})
    print(json.dumps({'release':str(OUT),'exe':str(exe),'zip':str(archive)},ensure_ascii=False))
if __name__=='__main__':main()
