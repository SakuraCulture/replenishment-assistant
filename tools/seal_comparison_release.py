"""Seal the immutable 0.30.0 candidate after independent and actual EXE checks."""
import argparse,hashlib,json,re,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def read(p):return json.loads(Path(p).read_text('utf-8'))

def main():
    p=argparse.ArgumentParser();p.add_argument('--qa',type=Path,required=True);p.add_argument('--delivery',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    qa=a.qa.resolve();delivery=a.delivery.resolve();out=a.output.resolve();meta=read(delivery/'版本信息.json');eh=sha(delivery/'补货助手.exe')
    if meta['version']!='0.30.0' or meta['model_version']!='0.3.4-rules':raise ValueError('Wrong candidate')
    eq=read(qa/'equivalence.json');archive=read(qa/'archive-exports/report.json');legacy=read(qa/'legacy-exports/report.json')
    if not all(r['passed'] for r in (eq,archive,legacy)) or eq['differences'] or archive['differences'] or legacy['differences'] or not archive['database_unchanged'] or not legacy['database_unchanged']:raise ValueError('Independent verification failed')
    protected=read(qa/'baseline.json')['protected']
    if any(sha(path)!=digest for path,digest in protected.items()):raise ValueError('Protected input or old package changed')
    for path,digest in read(qa/'after-final/capture.json')['source_hashes'].items():
        data=subprocess.check_output(['git','show',meta['source_commit']+':'+path.replace('\\','/')],cwd=ROOT)
        if sha(ROOT/path)!=digest or hashlib.sha256(data).hexdigest()!=digest:raise ValueError('Build commit or worktree differs from captured source: '+path)
    for name,count in [('unit.txt',580),('targeted-final.txt',14),('optimized-final.txt',14)]:
        if not re.search(r'Ran '+str(count)+r' tests.*?\nOK\s*$',(qa/name).read_text('utf-8-sig'),re.S):raise ValueError('Regression not passed: '+name)
    reports={'exe-comparison.json':qa/'exe-comparison/comparison-report.json','exe-readability.json':qa/'exe-readability/readability-report.json'}
    for path in reports.values():
        r=read(path)
        if not r['passed'] or not r['frozen_exe'] or r['source_commit']!=meta['source_commit'] or r['exe_sha256']!=eh or r.get('exit_code')!=0:raise ValueError('Actual EXE identity or result mismatch: '+str(path))
    if not read(qa/'environment.json')['passed']:raise ValueError('Environment verification failed')
    out.mkdir(parents=True,exist_ok=False)
    copies={**reports,'equivalence.json':qa/'equivalence.json','archive-exports.json':qa/'archive-exports/report.json','legacy-exports.json':qa/'legacy-exports/report.json','unit-tests.txt':qa/'unit.txt','comparison-tests.txt':qa/'targeted-final.txt','comparison-tests-optimized.txt':qa/'optimized-final.txt','environment.json':qa/'environment.json'}
    for name,path in copies.items():shutil.copy2(path,out/name)
    screenshots=out/'screenshots';screenshots.mkdir()
    for path in (qa/'exe-comparison').glob('*.png'):shutil.copy2(path,screenshots/path.name)
    files={p.relative_to(delivery).as_posix():{'bytes':p.stat().st_size,'sha256':sha(p)} for p in sorted(delivery.rglob('*')) if p.is_file()}
    zipped=delivery.parent/'补货助手_0.30.0_rc1_生命周期对比.zip'
    with zipfile.ZipFile(zipped,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for name in files:z.write(delivery/name,'补货助手/'+name)
    with zipfile.ZipFile(zipped) as z:
        if set(z.namelist())!={'补货助手/'+name for name in files}:raise ValueError('ZIP file set mismatch')
        for name,info in files.items():
            if hashlib.sha256(z.read('补货助手/'+name)).hexdigest()!=info['sha256']:raise ValueError('ZIP content mismatch')
    manifest={'version':meta['version'],'source_commit':meta['source_commit'],'exe_sha256':eh,'files':files,'zip':{'path':str(zipped),'bytes':zipped.stat().st_size,'sha256':sha(zipped)},'zip_reopened_all_files_verified':True,'protected_file_count':len(protected),'protected_unchanged':True}
    (out/'delivery-files.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({k:manifest[k] for k in ('source_commit','exe_sha256','zip','protected_file_count')},ensure_ascii=False))

if __name__=='__main__':main()
