"""Seal 0.36.1 only after source, actual EXE, CDP and protected-file checks."""
import hashlib,json,re,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def read(p):return json.loads(p.read_text('utf-8'))
def save(p,x):p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n','utf-8',newline='\n')

def main():
    qa=ROOT/'qa/v0361';delivery=ROOT/'交付_0.36.1_rc2/补货助手';release=ROOT/'releases/0.36.1/rc2'
    build=read(delivery/'_internal/build_info.json');eq=read(qa/'equivalence.json')
    if build['version']!='0.36.1' or not eq['passed'] or eq['difference_count']:raise ValueError('Wrong build or business comparison')
    exe=read(qa/'exe-rc2/report.json');ax=read(qa/'exe-aoxiang-r2/smoke.json')
    if not exe['passed'] or not exe['frozen'] or exe['process_exit'] or exe['version']['source_commit']!=build['source_commit'] or exe['exe_sha256']!=sha(delivery/'补货助手.exe'):raise ValueError('EXE evidence mismatch')
    if not ax['passed'] or not ax['actual_exe'] or ax['source_commit']!=build['source_commit']:raise ValueError('AX EXE evidence mismatch')
    for name in ['source-ui-r3/report.json','browser-source-r2/report.json','browser-bundle-r2/report.json']:
        if not read(qa/name)['passed']:raise ValueError('Failed '+name)
    from structure_oracle import source_hashes
    built=ROOT/'build_0.36.1_rc2/source'
    if source_hashes(ROOT)!=source_hashes(built) or source_hashes(ROOT)!=read(qa/'after-final2/capture.json')['source_hashes']:raise ValueError('Source changed after tests/build')
    raw=(qa/'python-tests.txt').read_text('utf-8')
    if not re.search(r'Ran 764 tests',raw) or not raw.rstrip().endswith('OK'):raise ValueError('Full regression not passed')
    for name,count in [('aoxiang-tests.txt',163),('jst-tests.txt',39)]:
        raw=(qa/name).read_text('utf-8')
        if not re.search(r'pass '+str(count)+r'\b',raw) or not re.search(r'fail 0\b',raw):raise ValueError('Component regression not passed '+name)
    baseline=read(qa/'baseline.json');documents={str(ROOT/n) for n in ['README.md','docs/项目意图与实施进度.md','docs/plans/业务使用体验优化与验收清单_20261104.md']}
    changed=[p for p,h in baseline['protected'].items() if p not in documents and (not Path(p).is_file() or sha(p)!=h)]
    if changed:raise ValueError('Protected data changed '+str(changed))
    release.mkdir(parents=True,exist_ok=False)
    copies={'equivalence.json':'equivalence.json','python-tests.txt':'python-tests.txt','aoxiang-tests.txt':'aoxiang-tests.txt','jst-tests.txt':'jst-tests.txt',
      'source-ui-r3/report.json':'source-ui.json','exe-rc2/report.json':'exe-ui.json','exe-aoxiang-r2/smoke.json':'exe-aoxiang.json',
      'exe-aoxiang-r2/shared-session.json':'exe-aoxiang-shared.json','browser-source-r2/report.json':'browser-source.json','browser-bundle-r2/report.json':'browser-bundle.json',
      'source-ui/report.json':'development-interface-failure.json','browser-source/report.json':'development-headless-limitation.json',
      'exe-aoxiang/smoke.json':'development-old-fixture-failure.json'}
    for source,target in copies.items():(release/target).write_bytes((qa/source).read_bytes().replace(b'\r\n',b'\n'))
    for name in ['before.png','after.png']:(release/name).write_bytes((qa/'exe-rc2'/name).read_bytes())
    save(release/'baseline.json',{'commit':baseline['baseline'],'source_zip_sha256':baseline['source_zip_sha256'],'protected_files':sum(p not in documents for p in baseline['protected']),'protected_changed':changed,'core':eq['core']})
    files={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    save(release/'files.json',{'build':build,'files':files})
    archive=delivery.parent/'补货助手_0.36.1_rc2_浏览器连接保留.zip'
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for name in files:z.write(delivery/name,'补货助手/'+name)
    with zipfile.ZipFile(archive) as z:
        if z.testzip() or len(z.namelist())!=len(files):raise ValueError('ZIP integrity failure')
        for name,digest in files.items():
            if hashlib.sha256(z.read('补货助手/'+name)).hexdigest()!=digest:raise ValueError('ZIP member mismatch')
    result={'version':'0.36.1','model_version':'0.3.4-rules','baseline':baseline['baseline'],'build_commit':build['source_commit'],
      'exe_sha256':sha(delivery/'补货助手.exe'),'zip_sha256':sha(archive),'archive':str(archive),'delivery':str(delivery),'files':len(files),
      'python_tests':764,'aoxiang_tests':163,'jst_tests':39,'business_differences':0,'cases_including_excel':len(eq['cases']),'protected_unchanged':True,
      'production_permission_prompt_tested':False,'overnight_tested':False,'pushed':False,'merged':False}
    save(release/'package.json',result);print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
