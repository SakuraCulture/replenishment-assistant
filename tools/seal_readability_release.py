"""Seal the 0.28.0 local candidate only after independent and frozen checks pass."""
import argparse,hashlib,json,re,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def read(p):return json.loads(Path(p).read_text('utf-8'))


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--qa',type=Path,required=True);p.add_argument('--delivery',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    qa=a.qa.resolve();delivery=a.delivery.resolve();out=a.output.resolve()
    meta=read(delivery/'版本信息.json');exe=delivery/'补货助手.exe';eh=sha(exe)
    if meta['version']!='0.28.0':raise ValueError('Wrong candidate')
    eq=read(qa/'equivalence.json');archive=read(qa/'archive-exports/report.json');perf=read(qa/'performance-final/report.json')
    if not eq['passed'] or eq['differences'] or not archive['passed'] or not perf['database_unchanged']:raise ValueError('Independent verification failed')
    protected={**read(qa/'baseline.json')['protected'],**eq['input_sha256']}
    if any(sha(path)!=digest for path,digest in protected.items()):raise ValueError('Protected input or old package changed')
    if any(sha(ROOT/path)!=digest for path,digest in read(qa/'after/capture.json')['source_hashes'].items()):raise ValueError('Source changed after capture')
    for path,digest in read(qa/'after/capture.json')['source_hashes'].items():
        data=subprocess.check_output(['git','show',meta['source_commit']+':'+path.replace('\\','/')],cwd=ROOT)
        if hashlib.sha256(data).hexdigest()!=digest:raise ValueError('Build commit differs from captured source')
    units=(qa/'unit-tests-final.txt').read_text('utf-8')
    if not re.search(r'Ran 552 tests.*?\nOK\s*$',units,re.S):raise ValueError('Full regression not passed')
    reports={'exe-readability.json':qa/'exe-readability/readability-report.json','exe-maintenance.json':qa/'exe-maintenance/maintenance-report.json','exe-parameters.json':qa/'exe-parameters/exe-verification.json'}
    for path in reports.values():
        r=read(path)
        if not r['passed'] or not r['frozen_exe'] or r['source_commit']!=meta['source_commit']:raise ValueError('Actual EXE result mismatch')
        if r['exe_sha256']!=eh or r.get('exit_code',r.get('process_exit'))!=0:raise ValueError('EXE verification identity or exit mismatch')
    out.mkdir(parents=True,exist_ok=False)
    copies={**reports,'equivalence.json':qa/'equivalence.json','archive-exports.json':qa/'archive-exports/report.json','performance.json':qa/'performance-final/report.json','unit-tests.txt':qa/'unit-tests-final.txt','readability-optimized.txt':qa/'readability-optimized.txt'}
    for name,path in copies.items():shutil.copy2(path,out/name)
    screenshots=out/'screenshots';screenshots.mkdir()
    for path in (qa/'exe-readability').glob('*.png'):shutil.copy2(path,screenshots/path.name)
    files={p.relative_to(delivery).as_posix():{'bytes':p.stat().st_size,'sha256':sha(p)} for p in sorted(delivery.rglob('*')) if p.is_file()}
    zipped=delivery.parent/'补货助手_0.28.0_rc1_业务可读性.zip'
    with zipfile.ZipFile(zipped,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for name in files:z.write(delivery/name,'补货助手/'+name)
    with zipfile.ZipFile(zipped) as z:
        if set(z.namelist())!={'补货助手/'+name for name in files}:raise ValueError('ZIP file set mismatch')
        for name,info in files.items():
            if hashlib.sha256(z.read('补货助手/'+name)).hexdigest()!=info['sha256']:raise ValueError('ZIP content mismatch')
    manifest={'version':meta['version'],'source_commit':meta['source_commit'],'exe_sha256':eh,'files':files,'zip':{'path':str(zipped),'bytes':zipped.stat().st_size,'sha256':sha(zipped)},'zip_reopened_all_files_verified':True,'protected_file_count':len(protected),'protected_unchanged':True}
    (out/'delivery-files.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:manifest[k] for k in ('source_commit','exe_sha256','zip','protected_file_count')},ensure_ascii=False))


if __name__=='__main__':main()
