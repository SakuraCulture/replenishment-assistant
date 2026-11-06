"""Seal independently verified 0.46.1 artifacts without replacing earlier evidence."""
import hashlib,json,re,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/current_storage_0461'
def read(p):return json.loads(p.read_text('utf-8-sig'))
def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def main():
    delivery=ROOT/'交付_0.46.1_rc1/补货助手';release=ROOT/'releases/0.46.1/rc1'
    build=read(ROOT/'build_0.46.1_rc1/build_info.json');assert build['version']=='0.46.1'
    changed=set(subprocess.check_output(['git','diff','--name-only',build['source_commit'],'--','src','tests','components','tools','prepare_delivery.py','requirements-win-amd64.lock'],cwd=ROOT,text=True).splitlines())
    assert changed<={'tools/verify_storage_exe.py','tools/seal_current_storage.py','tests/test_archive_orders.py'},changed
    validation_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    frozen=read(QA/'frozen.json')
    for path,digest in frozen['protected'].items():assert sha(Path(path))==digest,path
    regression=(QA/'regression-r3.log').read_text('utf-8-sig')
    assert re.search(r'Ran 1033 tests in [\d.]+s\s+OK\s*$',regression)
    for name in ('fixture-report.json','codec/report.json','backup/report.json','cold/report.json','warm/report.json','multiple/report.json','synthetic-performance/report.json'):
        assert read(QA/name)['passed'],name
    deletion=read(QA/'exe-deletion/deletion-report.json');storage=read(QA/'exe-storage-r2/storage_ui_report.json');recovery=read(QA/'exe-recovery/process-acceptance.json')
    assert deletion['passed'] and deletion['frozen_exe'] and deletion['process_exit']==0 and deletion['source_commit']==build['source_commit']
    assert storage['passed'] and not storage['development'] and storage['exit_code']==0 and not storage['remaining_children'] and storage['version']['source_commit']==build['source_commit']
    assert recovery['accepted'] and recovery['frozen_exe'] and recovery['source_commit']==build['source_commit']
    assert deletion['exe_sha256']==storage['exe_sha256']==sha(delivery/'补货助手.exe')
    from PyInstaller.archive.readers import ZlibArchiveReader
    modules=ZlibArchiveReader(str(ROOT/'build_0.46.1_rc1/work/补货助手/PYZ-00.pyz')).toc
    assert not any(name=='ijson' or name.startswith('ijson.') for name in modules)
    assert not any('ijson' in p.name.lower() for p in delivery.rglob('*'))
    release.mkdir(parents=True,exist_ok=False)
    files={'frozen.json':'frozen.json','fixture-report.json':'fixture-report.json','regression-r1.log':'regression-first-fixture-failure.txt','regression-r2.log':'regression-second-fixture-failure.txt','regression-r3.log':'regression.txt',
           'focused-r1.log':'focused-tests.txt','fixture-fix.log':'fixture-fix-tests.txt','codec/report.json':'codec.json','backup/report.json':'backup.json',
           'cold/report.json':'cold-performance.json','warm/report.json':'warm-performance.json','multiple/report.json':'multiple-performance.json','synthetic-performance/report.json':'synthetic-performance.json',
           'exe-deletion/deletion-report.json':'exe-deletion.json','exe-storage-r2/storage_ui_report.json':'exe-storage.json','exe-recovery/process-acceptance.json':'exe-recovery.json',
           'exe-storage-console.log':'exe-storage-first-harness-failure.txt','exe-storage/storage_ui_report.json':'exe-storage-first-ui-result.json'}
    for source,target in files.items():shutil.copyfile(QA/source,release/target)
    shutil.copyfile(ROOT/'build_0.46.1_rc1/build_info.json',release/'build_info.json')
    for name in ('exe-deletion','exe-storage-r2','exe-recovery'):
        dest=release/name;dest.mkdir()
        for p in (QA/name).glob('*.png'):shutil.copyfile(p,dest/p.name)
    manifest={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    target=delivery.parent/'补货助手_0.46.1_rc1_当前格式与采集修复.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as z:
        for name in manifest:z.write(delivery/name,'补货助手/'+name)
    with zipfile.ZipFile(target) as z:
        assert len(z.namelist())==len(manifest)
        for name,digest in manifest.items():assert hashlib.sha256(z.read('补货助手/'+name)).hexdigest()==digest,name
    result={'version':'0.46.1','source_commit':build['source_commit'],'validation_commit':validation_commit,'post_build_changes':sorted(changed),'exe_sha256':sha(delivery/'补货助手.exe'),'zip_sha256':sha(target),'files':manifest,'runtime_ijson_absent':True}
    (release/'manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps({'zip':str(target),'file_count':len(manifest),'zip_sha256':result['zip_sha256']},ensure_ascii=False))
if __name__=='__main__':main()
