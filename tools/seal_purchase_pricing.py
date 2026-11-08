"""Seal independent 0.48.7 pricing release; never overwrite old outputs."""
import hashlib,json,re,shutil,subprocess,zipfile
from pathlib import Path
from aoxiang_bundle import verify
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/purchase_price_0487'
def read(p):return json.loads(p.read_text('utf-8-sig'))
def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def write(p,value):
    with p.open('x',encoding='utf-8',newline='\n') as f:f.write(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
def main():
    build=read(ROOT/'build_0.48.7_rc1/build_info.json');commit=build['source_commit'];assert build['version']=='0.48.7'
    assert not subprocess.check_output(['git','diff',commit,'--','src','tests','components'],cwd=ROOT)
    raw=(QA/'run1/regression2.log').read_text('utf-8-sig');m=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$',raw);assert m and int(m[1])>=1264
    ids=re.findall(r'^(test\w+ \([^\n]+\)) \.\.\.',raw,re.M);assert len(ids)==len(set(ids))==int(m[1])
    focused=(QA/'run1/focused3.log').read_text('utf-8-sig');assert re.search(r'Ran 138 tests .*\s+OK\s*$',focused)
    node=(QA/'run1/node-all2.log').read_text('utf-8-sig');assert re.search(r'(?:#|ℹ) tests 193',node) and re.search(r'(?:#|ℹ) pass 193',node) and re.search(r'(?:#|ℹ) fail 0',node) and re.search(r'(?:#|ℹ) skipped 0',node)
    exe=read(QA/'exe1/report.json');source=read(QA/'source2/report.json');probe=read(QA/'packaged-component.json')
    assert exe['passed'] and source['passed'] and len(exe['checks'])==9 and exe['source_commit']==commit and exe['live_platform'] is False
    assert probe['passed'] and probe['live_platform'] is False
    previous=read(ROOT/'releases/0.48.6/rc1/frozen.json');old=read(ROOT/'releases/0.48.6/rc1/validation.json')
    protected=dict(previous['preserved_deliveries']);protected['交付_0.48.6_rc1/补货助手/补货助手.exe']=old['exe_sha256'];protected[old['zip']]=old['zip_sha256']
    assert all(sha(ROOT/p)==h for p,h in previous['core'].items());assert all(sha(ROOT/p)==h for p,h in protected.items())
    assert not subprocess.check_output(['git','diff','v0.48.6-rc1',commit,'--','components/jst_catalog'],cwd=ROOT)
    delivery=ROOT/'交付_0.48.7_rc1/补货助手';manifest=verify(delivery/'_internal/components/aoxiang')
    for name in ['purchase-create-contract.mjs','purchase-create-artifacts.mjs','purchase-create-service.mjs','purchase-binding.mjs']:
        assert sha(delivery/'_internal/components/aoxiang/src'/name)==sha(ROOT/'build_0.48.7_rc1/source/components/aoxiang/src'/name)
    with (delivery/'使用手册/0.48.7采购单价使用说明.txt').open('xb') as f:f.write((ROOT/'build_0.48.7_rc1/source/docs/0.48.7采购单价两位小数.md').read_bytes())
    release=ROOT/'releases/0.48.7/rc1';release.mkdir(parents=True,exist_ok=False)
    for name,p in {'build_info.json':ROOT/'build_0.48.7_rc1/build_info.json','regression.log':QA/'run1/regression2.log','focused-tests.log':QA/'run1/focused3.log','node-tests.log':QA/'run1/node-all2.log','exe-report.json':QA/'exe1/report.json','packaged-component.json':QA/'packaged-component.json','source-report.json':QA/'source2/report.json','purchase-preview.png':QA/'exe1/purchase-preview.png'}.items():shutil.copyfile(p,release/name)
    (release/'retained-failures').mkdir()
    shutil.copyfile(QA/'run1/focused1.log',release/'retained-failures/focused1.log')
    hashes={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()};target=delivery.parent/'补货助手_0.48.7_rc1_采购单价两位小数.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as z:
        for name in hashes:z.write(delivery/name,'补货助手/'+name)
    with zipfile.ZipFile(target) as z:
        assert len(z.namelist())==len(hashes)
        for name,h in hashes.items():
            with z.open('补货助手/'+name) as f:assert hashlib.file_digest(f,'sha256').hexdigest()==h
    write(release/'manifest.json',{'source_commit':commit,'files':hashes});write(release/'frozen.json',{'core':previous['core'],'preserved_deliveries':protected})
    result={'passed':True,'source_commit':commit,'python_tests':int(m[1]),'unique_test_ids':len(set(ids)),'python_seconds':float(m[2]),'focused_tests':138,'node_tests':193,'exe_checks':len(exe['checks']),'live_platform':False,'core_unchanged':len(previous['core']),'preserved_delivery_files':len(protected),'component_files_verified':len(manifest['files']),'exe_sha256':sha(delivery/'补货助手.exe'),'zip':str(target.relative_to(ROOT)),'zip_sha256':sha(target),'files':len(hashes),'zip_bytes':target.stat().st_size}
    write(release/'validation.json',result);print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
