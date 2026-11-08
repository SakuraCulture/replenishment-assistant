"""Seal 0.48.4 isolated layout and signing guidance evidence and verify every ZIP member."""
import hashlib,json,re,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/layout_0484'
def read(p):return json.loads(p.read_text('utf-8-sig'))
def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def write(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n','utf-8',newline='\n')

def main():
    build=read(ROOT/'build_0.48.4_rc2/build_info.json');commit=build['source_commit']
    assert build['version']=='0.48.4'
    assert not subprocess.check_output(['git','diff',commit,'--','src','tests'],cwd=ROOT)
    assert not subprocess.check_output(['git','diff','v0.48.3-rc2',commit,'--','components'],cwd=ROOT)
    raw=(QA/'run1/regression-final.log').read_text('utf-8-sig')
    match=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$',raw)
    assert match and int(match[1])>=1245
    ids=re.findall(r'^(test\w+ \([^\n]+\)) \.\.\.',raw,re.M)
    assert len(ids)==len(set(ids))==int(match[1]),'Duplicate or missing test identities'
    assert re.search(r'Ran 72 tests .*\s+OK\s*$',(QA/'run1/focused2.log').read_text('utf-8-sig'))
    exe=read(QA/'exe2/report.json');assert exe['passed'] and exe['source_commit']==commit and exe['live_platform'] is False
    assert read(QA/'source4/report.json')['passed']
    filters=read(QA/'filters-exe2/report.json');assert filters['passed'] and filters['source_commit']==commit
    previous=read(ROOT/'releases/0.48.3/rc2/frozen.json');old=read(ROOT/'releases/0.48.3/rc2/validation.json')
    protected=dict(previous['preserved_deliveries']);protected['交付_0.48.3_rc2/补货助手/补货助手.exe']=old['exe_sha256'];protected[old['zip']]=old['zip_sha256']
    assert all(sha(ROOT/p)==h for p,h in previous['core'].items())
    assert all(sha(ROOT/p)==h for p,h in protected.items())
    release=ROOT/'releases/0.48.4/rc2';release.mkdir(parents=True,exist_ok=False)
    for name,p in {'build_info.json':ROOT/'build_0.48.4_rc2/build_info.json','regression.log':QA/'run1/regression-final.log',
                   'focused-tests.log':QA/'run1/focused2.log','source-ui.json':QA/'source4/report.json','exe-report.json':QA/'exe2/report.json'}.items():shutil.copyfile(p,release/name)
    (release/'retained-failures').mkdir();shutil.copyfile(QA/'source1/report.json',release/'retained-failures/fixture-missing-intake.json')
    shutil.copyfile(QA/'exe1/report.json',release/'retained-failures/native-small-window-table-too-short.json')
    (release/'screenshots').mkdir()
    shutil.copyfile(QA/'filters-exe2/report.json',release/'filters-exe.json')
    for p in (QA/'exe2').glob('*.png'):shutil.copyfile(p,release/'screenshots'/p.name)
    delivery=ROOT/'交付_0.48.4_rc2/补货助手'
    addendum=delivery/'使用手册/0.48.3新增功能使用说明.txt'
    with addendum.open('xb') as stream:stream.write((ROOT/'build_0.48.4_rc2/source/docs/0.48.3最终清单筛选与批量加减.md').read_bytes())
    with (delivery/'使用手册/0.48.4新增功能使用说明.txt').open('xb') as stream:stream.write((ROOT/'build_0.48.4_rc2/source/docs/0.48.4表格空间与签发引导.md').read_text('utf-8').replace('releases/0.48.4/rc1','releases/0.48.4/rc2').encode('utf-8'))
    hashes={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    target=delivery.parent/'补货助手_0.48.4_rc2_主表格布局与签发引导.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as z:
        for name in hashes:z.write(delivery/name,'补货助手/'+name)
    with zipfile.ZipFile(target) as z:
        assert len(z.namelist())==len(hashes)
        for name,h in hashes.items():
            with z.open('补货助手/'+name) as f:assert hashlib.file_digest(f,'sha256').hexdigest()==h
    write(release/'manifest.json',{'source_commit':commit,'files':hashes})
    write(release/'frozen.json',{'core':previous['core'],'preserved_deliveries':protected})
    result={'passed':True,'source_commit':commit,'python_tests':int(match[1]),'unique_test_ids':len(set(ids)),'python_seconds':float(match[2]),
            'focused_tests':72,'exe_checks':len(exe['checks']),'exe_distinct_checks':len(set(exe['checks'])),
            'exe_sha256':sha(delivery/'补货助手.exe'),'zip':str(target.relative_to(ROOT)),'zip_sha256':sha(target),'zip_bytes':target.stat().st_size,
            'files':len(hashes),'core_unchanged':len(previous['core']),'preserved_delivery_files':len(protected),'components_unchanged':True,'live_platform':False,'geometry':exe['geometry']}
    write(release/'validation.json',result);print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
