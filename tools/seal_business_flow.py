"""Seal this iteration only after strict source, EXE and protected-data checks."""
import hashlib,json,re,shutil,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def read(p):return json.loads(p.read_text('utf-8'))
def save(p,x):p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n','utf-8',newline='\n')
def main():
    qa=ROOT/'qa/v0360';release=ROOT/'releases/0.36.0/rc1';delivery=ROOT/'交付_0.36.0_rc1/补货助手'
    build=read(delivery/'_internal/build_info.json');eq=read(qa/'equivalence.json')
    if not eq['passed'] or eq['difference_count']:raise ValueError('Equivalence not passed')
    reports={}
    for folder,name in [('exe-rc1','business-flow-report.json'),('exe-narrow-rc1','business-flow-report.json'),('exe-parameters-rc1','ui-report.json')]:
        r=read(qa/folder/name);reports[folder]=r
        if not (r.get('passed') or r.get('accepted')) or not r['frozen_exe'] or r['build_commit']!=build['source_commit']:raise ValueError('Wrong/failed actual EXE report '+folder)
    from structure_oracle import source_hashes
    built=ROOT/f"build_{build['version']}_{build['build_id']}/source"
    if source_hashes(ROOT)!=source_hashes(built) or source_hashes(ROOT)!=read(qa/'after/capture.json')['source_hashes']:raise ValueError('Source differs from build or comparison')
    text=(qa/'full-tests-final.txt').read_text('utf-8')
    if not re.search(r'Ran 754 tests',text) or not text.rstrip().endswith('OK'):raise ValueError('Full regression not passed')
    for file in ('performance/report.json','storage-roundtrip-2/report.json','completed-receipt-replay-2/report.json'):
        if not read(qa/file)['passed']:raise ValueError('Evidence not passed '+file)
    baseline=read(qa/'baseline.json');docs={str(ROOT/n) for n in ['README.md','docs/项目意图与实施进度.md','docs/plans/业务使用体验优化与验收清单_20261104.md']}
    changed=[p for p,h in baseline['protected'].items() if p not in docs and (not Path(p).is_file() or sha(p)!=h)]
    if changed:raise ValueError('Protected data changed: '+str(changed))
    release.mkdir(parents=True,exist_ok=False)
    evidence=[('equivalence.json','equivalence.json'),('full-tests-final.txt','full-tests.txt'),('scope-tests.txt','scope-tests.txt'),
              ('performance/report.json','performance.json'),('storage-roundtrip-2/report.json','storage-roundtrip.json'),
              ('completed-receipt-replay-2/report.json','completed-receipt.json')]
    for folder,name in [('exe-rc1','business-flow-report.json'),('exe-narrow-rc1','business-flow-report.json'),('exe-parameters-rc1','ui-report.json')]:evidence.append((folder+'/'+name,folder+'.json'))
    for origin,name in evidence:
        (release/name).write_bytes((qa/origin).read_bytes().replace(b'\r\n',b'\n'))
    for file in (qa/'exe-rc1').glob('*.png'):shutil.copy2(file,release/file.name)
    for file in (qa/'exe-narrow-rc1').glob('*.png'):shutil.copy2(file,release/('窄窗口-'+file.name))
    save(release/'baseline.json',{'commit':baseline['baseline'],'source_zip_sha256':baseline['source_zip_sha256'],
        'protected_files':len(baseline['protected'])-len(docs),'protected_changed':changed,'core':eq['core']})
    manifest={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    save(release/'files.json',{'build':build,'files':manifest})
    archive=delivery.parent/'补货助手_0.36.0_rc1_业务待办与核对.zip'
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for name in manifest:z.write(delivery/name,'补货助手/'+name)
    with zipfile.ZipFile(archive) as z:
        if z.testzip():raise ValueError('ZIP CRC failure')
        for name,digest in manifest.items():
            if hashlib.sha256(z.read('补货助手/'+name)).hexdigest()!=digest:raise ValueError('ZIP member mismatch')
    result={'version':'0.36.0','model_version':'0.3.4-rules','baseline':baseline['baseline'],'build_commit':build['source_commit'],
        'exe_sha256':sha(delivery/'补货助手.exe'),'zip_sha256':sha(archive),'archive':str(archive),'delivery':str(delivery),'files':len(manifest),
        'tests':754,'business_differences':0,'cases':len(eq['cases']),'protected_unchanged':True,'AI_client_acceptance':'deferred by user',
        'live_receipt_sample_count':1,'historic_production_binding_verified':False,'pushed':False,'merged':False}
    save(release/'package.json',result);print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
