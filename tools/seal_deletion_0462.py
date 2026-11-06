"""Seal the measured 0.46.2 candidate; never replace a prior delivery or receipt."""
import hashlib,json,re,shutil,subprocess,zipfile
from pathlib import Path
from run_deletion_0462 import CASES
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/deletion_0462'
def read(path):return json.loads(path.read_text('utf-8-sig'))
def sha(path):
    with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def main():
    release=ROOT/'releases/0.46.2/rc1';delivery=ROOT/'交付_0.46.2_rc1/补货助手'
    build=read(ROOT/'build_0.46.2_rc1/build_info.json');environment=read(QA/'environment.json')
    assert build['version']=='0.46.2' and build['source_commit']==environment['commit']
    assert not subprocess.check_output(['git','diff',build['source_commit'],'--','src','tests','tools','components','prepare_delivery.py'],cwd=ROOT)
    for path,h in environment['measured_sources'].items():assert sha(ROOT/path)==h,path
    for path,h in read(QA/'frozen.json')['protected'].items():assert sha(Path(path))==h,path
    for file,count in [('regression.txt',1043),('stream-tests-r1.txt',48)]:
        assert re.search(r'Ran '+str(count)+r' tests in [\d.]+s\s+OK\s*$',(QA/file).read_text('utf-8-sig')),file
    differential=read(QA/'differential/report.json');assert differential['passed'] and len(differential['cases'])==48
    for name,_,rounds,*_ in CASES:
        report=read(QA/name/'report.json')
        assert report['passed'] and not report['differences'] and not report['changed_protected'] and len(report['runs'])==2*rounds,name
        assert read(QA/name/'audit.json')['passed'],name
    deletion=read(QA/'exe-deletion/deletion-report.json');backup=read(QA/'exe-storage/storage_ui_report.json');recovery=read(QA/'exe-recovery/process-acceptance.json')
    assert deletion['passed'] and deletion['frozen_exe'] and deletion['process_exit']==0 and len(deletion['checks'])==8
    assert backup['passed'] and not backup['development'] and backup['exit_code']==0 and not backup['remaining_children'] and len(backup['checks'])==7
    assert recovery['accepted'] and recovery['frozen_exe'] and len(recovery['checks'])==10
    assert deletion['source_commit']==backup['version']['source_commit']==recovery['source_commit']==build['source_commit']
    assert deletion['exe_sha256']==backup['exe_sha256']==sha(delivery/'补货助手.exe')
    release.mkdir(parents=True,exist_ok=False)
    for name,*_ in CASES:
        shutil.copyfile(QA/name/'report.json',release/(name+'-performance.json'))
        shutil.copyfile(QA/name/'audit.json',release/(name+'-audit.json'))
    for name in ('frozen.json','environment.json','inputs.json','native-fixture.json','native-cache.json','regression.txt','stream-tests-r1.txt'):
        shutil.copyfile(QA/name,release/name)
    shutil.copyfile(QA/'differential/report.json',release/'differential.json')
    shutil.copyfile(ROOT/'build_0.46.2_rc1/build_info.json',release/'build_info.json')
    for folder,report in [('exe-deletion','deletion-report.json'),('exe-storage','storage_ui_report.json'),('exe-recovery','process-acceptance.json')]:
        shutil.copyfile(QA/folder/report,release/(folder+'.json'));dest=release/folder;dest.mkdir()
        for path in (QA/folder).glob('*.png'):shutil.copyfile(path,dest/path.name)
    pilots=release/'exploratory';pilots.mkdir()
    for name in ('cache-pilot','stream-pilot','native-pilot'):shutil.copyfile(QA/name/'report.json',pilots/(name+'.json'))
    (pilots/'README.txt').write_text('Exploratory single pairs only. cache-pilot changed cache staging alone; stream-pilot and native-pilot also enabled fused rewriting. Final claims use the seven separately rerun matrices from the immutable build commit.',encoding='utf-8')
    patch=subprocess.check_output(['git','diff',read(QA/'frozen.json')['commit'],build['source_commit'],'--','src'],cwd=ROOT)
    (release/'runtime.patch').write_bytes(patch)
    files={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    target=delivery.parent/'补货助手_0.46.2_rc1_删除流式处理优化.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as z:
        for name in files:z.write(delivery/name,'补货助手/'+name)
    with zipfile.ZipFile(target) as z:
        assert len(z.namelist())==len(files)
        for name,h in files.items():assert hashlib.sha256(z.read('补货助手/'+name)).hexdigest()==h,name
    manifest={'version':'0.46.2','build_id':'rc1','source_commit':build['source_commit'],
              'exe_sha256':sha(delivery/'补货助手.exe'),'zip_sha256':sha(target),'files':files}
    (release/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'zip':str(target),'files':len(files),'sha256':manifest['zip_sha256']},ensure_ascii=False))
if __name__=='__main__':main()
