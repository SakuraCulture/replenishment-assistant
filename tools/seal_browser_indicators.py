"""Seal the 0.36.2 candidate after regression, frozen UI and isolated real Edge checks."""
import hashlib,json,re,zipfile
from pathlib import Path
from structure_oracle import source_hashes

ROOT=Path(__file__).resolve().parents[1]
def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def read(p):return json.loads(p.read_text('utf-8'))
def save(p,value):p.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n','utf-8',newline='\n')

def main():
    qa=ROOT/'qa/v0362';delivery=ROOT/'交付_0.36.2_rc1/补货助手';release=ROOT/'releases/0.36.2/rc1'
    build=read(delivery/'_internal/build_info.json');eq=read(qa/'equivalence.json')
    if build['version']!='0.36.2' or not eq['passed'] or eq['difference_count']:raise ValueError('Wrong build or business comparison')
    for name in ['exe-indicators/report.json','exe-catalog/report.json']:
        report=read(qa/name)
        if not report['passed'] or not report['frozen'] or report['process_exit'] or report['version']['source_commit']!=build['source_commit'] or report['exe_sha256']!=sha(delivery/'补货助手.exe'):raise ValueError('EXE evidence mismatch '+name)
    ax=read(qa/'exe-aoxiang/smoke.json')
    if not ax['passed'] or not ax['actual_exe'] or ax['source_commit']!=build['source_commit']:raise ValueError('AX EXE evidence mismatch')
    for name in ['source-ui/report.json','source-catalog/report.json','browser-bundle/report.json']:
        if not read(qa/name)['passed']:raise ValueError('Failed '+name)
    built=ROOT/'build_0.36.2_rc1/source'
    if source_hashes(ROOT)!=source_hashes(built) or source_hashes(ROOT)!=read(qa/'after/capture.json')['source_hashes']:raise ValueError('Source changed after tests/build')
    raw=(qa/'python-tests.txt').read_text('utf-8')
    if not re.search(r'Ran 774 tests',raw) or not raw.rstrip().endswith('OK'):raise ValueError('Full regression not passed')
    for name,count in [('aoxiang-tests-r2.txt',167),('jst-tests.txt',40)]:
        raw=(qa/name).read_text('utf-8')
        if not re.search(r'pass '+str(count)+r'\b',raw) or not re.search(r'fail 0\b',raw):raise ValueError('Component regression not passed '+name)
    baseline=read(qa/'baseline.json');documents={str(ROOT/n) for n in ['README.md','docs/项目意图与实施进度.md','docs/plans/业务使用体验优化与验收清单_20261104.md']}
    changed=[p for p,h in baseline['protected'].items() if p not in documents and (not Path(p).is_file() or sha(p)!=h)]
    if changed:raise ValueError('Protected data changed '+str(changed))
    release.mkdir(parents=True,exist_ok=False)
    copies={'equivalence.json':'equivalence.json','python-tests.txt':'python-tests.txt','aoxiang-tests-r2.txt':'aoxiang-tests.txt','jst-tests.txt':'jst-tests.txt',
        'aoxiang-tests.txt':'development-old-fixture-failure.txt','indicator-tests.txt':'indicator-tests.txt','connection-tests.txt':'connection-tests.txt',
        'source-ui/report.json':'source-indicators.json','source-catalog/report.json':'source-catalog.json','exe-indicators/report.json':'exe-indicators.json',
        'exe-catalog/report.json':'exe-catalog.json','exe-aoxiang/smoke.json':'exe-aoxiang.json','exe-aoxiang/shared-session.json':'exe-aoxiang-shared.json',
        'browser-bundle/report.json':'browser-bundle.json'}
    for source,target in copies.items():(release/target).write_bytes((qa/source).read_bytes().replace(b'\r\n',b'\n'))
    for name in ['connected-and-disconnected.png','unknown.png']:(release/name).write_bytes((qa/'exe-indicators'/name).read_bytes())
    save(release/'baseline.json',{'commit':baseline['baseline'],'source_zip_sha256':baseline['source_zip_sha256'],'protected_files':sum(p not in documents for p in baseline['protected']),'protected_changed':changed,'core':eq['core']})
    files={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    save(release/'files.json',{'build':build,'files':files})
    archive=delivery.parent/'补货助手_0.36.2_rc1_底部连接状态.zip'
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for name in files:z.write(delivery/name,'补货助手/'+name)
    with zipfile.ZipFile(archive) as z:
        if z.testzip() or len(z.namelist())!=len(files):raise ValueError('ZIP integrity failure')
        for name,digest in files.items():
            if hashlib.sha256(z.read('补货助手/'+name)).hexdigest()!=digest:raise ValueError('ZIP member mismatch')
    result={'version':'0.36.2','model_version':'0.3.4-rules','baseline':baseline['baseline'],'build_commit':build['source_commit'],
      'exe_sha256':sha(delivery/'补货助手.exe'),'zip_sha256':sha(archive),'archive':str(archive),'delivery':str(delivery),'files':len(files),
      'python_tests':774,'aoxiang_tests':167,'jst_tests':40,'business_differences':0,'cases_including_excel':len(eq['cases']),'protected_unchanged':True,
      'production_permission_prompt_tested':False,'website_login_validity_monitored':False,'overnight_tested':False,'pushed':False,'merged':False}
    save(release/'package.json',result);print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
