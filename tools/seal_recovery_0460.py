"""Seal verified local 0.46.0 artifacts; refuse any existing release or ZIP."""
import hashlib,json,re,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/recovery_0460'
def read(p):return json.loads(p.read_text('utf-8'))
def sha(p):
    with p.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
def main():
    delivery=ROOT/'交付_0.46.0_rc1/补货助手';release=ROOT/'releases/0.46.0/rc1'
    build=read(ROOT/'build_0.46.0_rc1/build_info.json')
    assert build['version']=='0.46.0'
    assert not subprocess.check_output(['git','diff',build['source_commit'],'--','src','components','tools'],cwd=ROOT)
    baseline=read(QA/'baseline.json');assert not baseline['cold_equivalence_differences']
    for name,expected in baseline['protected'].items():assert sha(Path(name))==expected,name
    regression=(QA/'regression-r1.log').read_text('utf-8-sig')
    assert re.search(r'Ran 1025 tests in [\d.]+s\s+OK\s*$',regression)
    node=(QA/'node-all-r2.log').read_text('utf-8-sig')
    assert re.search(r'pass 217\b',node) and re.search(r'fail 0\b',node)
    recovery=read(QA/'exe-recovery/process-acceptance.json');maintenance=read(QA/'exe-collection/process-acceptance.json')
    for report in (recovery,maintenance):assert report['accepted'] and report['frozen_exe'] and report['source_commit']==build['source_commit']
    deletion=read(QA/'exe-deletion/deletion-report.json')
    assert deletion['passed'] and deletion['frozen_exe'] and deletion['process_exit']==0 and deletion['source_commit']==build['source_commit']
    assert deletion['exe_sha256']==sha(delivery/'补货助手.exe')
    catalog=read(QA/'exe-catalog/report.json');assert catalog['passed'] and catalog['frozen'] and catalog['process_exit']==0
    browser=read(QA/'browser-exe/report.json');assert browser['passed'] and browser['closed']==[True,True]
    for relative in ('aoxiang/src/collection-recovery.mjs','aoxiang/src/machine-runtime.mjs','aoxiang/src/machine-maintenance.mjs','aoxiang/src/machine-commands.mjs','jst_catalog/src/accounts.mjs','jst_catalog/src/service.mjs'):
        assert sha(ROOT/'components'/relative)==sha(delivery/'_internal/components'/relative),relative
    release.mkdir(parents=True,exist_ok=False)
    files={'baseline.json':'baseline.json','regression-r1.log':'regression.txt','node-all-r2.log':'node-tests.txt',
      'deletion-cold-profile/profile.json':'deletion-profile.json','deletion-cold-profile/result.json':'deletion-result.json',
      'exe-recovery/process-acceptance.json':'exe-recovery.json','exe-collection/process-acceptance.json':'exe-collection.json',
      'exe-deletion/deletion-report.json':'exe-deletion.json','exe-catalog/report.json':'exe-catalog.json','browser-exe/report.json':'browser-exe.json',
      'browser-r1/report.json':'browser-first-failure.json','focused-node-r1.log':'focused-node-first-failure.txt','focused-node-r2.log':'focused-node-second-failure.txt'}
    for source,target in files.items():shutil.copyfile(QA/source,release/target)
    shutil.copyfile(ROOT/'build_0.46.0_rc1/build_info.json',release/'build_info.json')
    for name in ('exe-recovery','exe-catalog','exe-deletion'):
        folder=release/name;folder.mkdir()
        for image in (QA/name).glob('*.png'):shutil.copyfile(image,folder/image.name)
    manifest={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    target=delivery.parent/'补货助手_0.46.0_rc1_采集恢复与自动配置.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as z:
        for name in manifest:z.write(delivery/name,'补货助手/'+name)
    with zipfile.ZipFile(target) as z:
        assert len(z.namelist())==len(manifest)
        for name,digest in manifest.items():assert hashlib.sha256(z.read('补货助手/'+name)).hexdigest()==digest,name
    result={'version':'0.46.0','source_commit':build['source_commit'],'exe_sha256':sha(delivery/'补货助手.exe'),'zip_sha256':sha(target),'files':manifest}
    (release/'manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps({'zip':str(target),'file_count':len(manifest),'zip_sha256':result['zip_sha256']},ensure_ascii=False))
if __name__=='__main__':main()
