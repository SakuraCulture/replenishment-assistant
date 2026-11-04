"""Seal the verified 0.34.0 candidate; existing packages are never overwritten."""
import hashlib,json,re,shutil,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def read(p):return json.loads(p.read_text('utf-8'))
def save(p,x):p.write_text(json.dumps(x,ensure_ascii=False,indent=2),encoding='utf-8')
def main():
    qa=ROOT/'qa/v0340';release=ROOT/'releases/0.34.0/rc2';release.mkdir(parents=True,exist_ok=True)
    delivery=ROOT/'交付_0.34.0_rc2/补货助手';build=read(delivery/'_internal/build_info.json');exe=read(qa/'exe-rc2/acceptance.json');eq=read(qa/'equivalence.json')
    if not exe['accepted'] or not exe['frozen_exe'] or exe['build_commit']!=build['source_commit']:raise ValueError('Actual EXE acceptance mismatch')
    if not eq['passed'] or eq['difference_count']:raise ValueError('Business equivalence failed')
    for name,n in [('all-python-rc2.txt',716),('parameter-tests-final.txt',20)]:
        raw=(qa/name).read_text('utf-8')
        if not re.search(r'Ran '+str(n)+r' tests',raw) or not raw.rstrip().endswith('OK'):raise ValueError('Regression not passed: '+name)
        shutil.copy2(qa/name,release/name)
    baseline=read(qa/'baseline.json');external={str(ROOT/n) for n in ['README.md','docs/项目意图与实施进度.md','docs/plans/业务使用体验优化与验收清单_20261104.md']}
    changed=[p for p,h in baseline['protected'].items() if p not in external and (not Path(p).exists() or sha(p)!=h)]
    if changed:raise ValueError('Protected input/package/user data changed: '+str(changed))
    for src,dst in [('equivalence.json','equivalence.json'),('exe-rc2/acceptance.json','exe-acceptance.json'),('exe-rc2/01-参数入口.png','01-参数入口.png'),('exe-rc2/02-勾选字段.png','02-勾选字段.png'),('exe-rc2/03-全局映射.png','03-全局映射.png')]:shutil.copy2(qa/src,release/dst)
    save(release/'baseline.json',{'commit':baseline['baseline'],'archive_sha256':baseline['source_zip_sha256'],'protected_file_count':len(baseline['protected'])-len(external),'protected_changed':changed,'external_document_changes':eq['external_document_changes'],'core':eq['core']})
    files=sorted(p for p in delivery.rglob('*') if p.is_file());manifest={p.relative_to(delivery).as_posix():sha(p) for p in files}
    save(release/'delivery-manifest.json',{'build_commit':build['source_commit'],'files':manifest})
    archive=delivery.parent/'补货助手_0.34.0_rc2_参数分组与预案.zip'
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for p in files:z.write(p,'补货助手/'+p.relative_to(delivery).as_posix())
    with zipfile.ZipFile(archive) as z:
        if z.testzip() is not None or len(z.namelist())!=len(files):raise ValueError('ZIP integrity mismatch')
        for name,h in manifest.items():
            if hashlib.sha256(z.read('补货助手/'+name)).hexdigest()!=h:raise ValueError('ZIP member mismatch: '+name)
    record={'version':'0.34.0','model_version':'0.3.4-rules','baseline':baseline['baseline'],'build_commit':build['source_commit'],'delivery':str(delivery),'archive':str(archive),'files':len(files),'exe_sha256':sha(delivery/'补货助手.exe'),'zip_sha256':sha(archive),'python_regressions':716,'new_parameter_tests':20,'ordered_comparison_cases':len(eq['cases']),'business_differences':0,'exe_checks':exe['checks'],'old_delivery_unchanged':True,'user_database_unchanged':True,'live_platform_tested':False}
    save(release/'package.json',record);print(json.dumps(record,ensure_ascii=False))
if __name__=='__main__':main()
