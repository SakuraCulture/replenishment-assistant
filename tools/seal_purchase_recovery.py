"""Seal the verified 0.46.3 candidate into new local release/ZIP paths only."""
import hashlib,json,re,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
QA=ROOT/'qa/purchase_recovery_0463'
BASE='9448b888f24a2561cd614da439f5d27a17e0ff77'

def read(p):return json.loads(p.read_text('utf-8'))
def sha(p):
    with p.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
def git(*args):return subprocess.check_output(['git',*args],cwd=ROOT)

def main():
    build=read(ROOT/'build_0.46.3_rc1/build_info.json');commit=build['source_commit']
    delivery=ROOT/'交付_0.46.3_rc1/补货助手';release=ROOT/'releases/0.46.3/rc1'
    assert build['version']=='0.46.3'
    assert not git('diff',commit,'--','src','components','tools')
    regression=(QA/'regression-r1.log').read_text('utf-8-sig')
    test=re.search(r'Ran (\d+) tests in [\d.]+s\s+OK\s*$',regression)
    assert test and int(test[1])>=1057
    assert re.search(r'Ran 13 tests in [\d.]+s\s+OK\s*$',(QA/'focused-r1.log').read_text('utf-8-sig'))
    node=(QA/'node-r1.log').read_text('utf-8-sig')
    assert re.search(r'pass 172\b',node) and re.search(r'fail 0\b',node)
    exe=read(QA/'exe-r1/process-acceptance.json')
    assert exe['accepted'] and exe['frozen_exe'] and exe['source_commit']==commit
    assert len(exe['checks'])==9 and exe['synthetic_uploads']==0 and exe['process_exit']==0
    assert exe['exe_sha256']==sha(delivery/'补货助手.exe')
    protected=['src/model.py','src/engine.py','src/allocation.py','src/parameter_schema.py','src/default_config.json',
               'src/default_must_sell.json','src/warehouse_catalog.py','src/data_maintenance.py','src/maintenance_storage.py']
    audit={}
    for path in protected:
        before=hashlib.sha256(git('show',BASE+':'+path)).hexdigest();after=sha(ROOT/path)
        assert before==after,path
        audit[path]={'baseline_sha256':before,'candidate_sha256':after}
    assert not git('diff',BASE,commit,'--','components')
    old=ROOT/'交付_0.46.2_rc1/补货助手/补货助手.exe'
    assert sha(old)=='b8ad66d732839872e30049b51a220363e14c94ef90237befd508f1fdf9f15993'
    release.mkdir(parents=True,exist_ok=False)
    for source,target in [('regression-r1.log','regression.txt'),('focused-r1.log','focused.txt'),('node-r1.log','node-tests.txt'),
                          ('source-ui-r2/process-acceptance.json','source-ui.json'),('exe-r1/process-acceptance.json','exe-report.json')]:
        shutil.copyfile(QA/source,release/target)
    shutil.copyfile(ROOT/'build_0.46.3_rc1/build_info.json',release/'build_info.json')
    images=release/'exe-screenshots';images.mkdir()
    for p in (QA/'exe-r1').glob('*.png'):shutil.copyfile(p,images/p.name)
    (release/'runtime.patch').write_bytes(git('diff',BASE,commit,'--','src'))
    (release/'source-audit.json').write_text(json.dumps({'baseline':BASE,'source_commit':commit,'protected':audit,
        'components_unchanged':True,'old_0462_exe_unchanged':True,'python_tests':int(test[1])},ensure_ascii=False,indent=2),'utf-8')
    manifest={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    target=delivery.parent/'补货助手_0.46.3_rc1_采购恢复与异常说明.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as z:
        for name in manifest:z.write(delivery/name,'补货助手/'+name)
    with zipfile.ZipFile(target) as z:
        assert len(z.namelist())==len(manifest)
        for name,digest in manifest.items():assert hashlib.sha256(z.read('补货助手/'+name)).hexdigest()==digest,name
    result={'version':'0.46.3','source_commit':commit,'exe_sha256':sha(delivery/'补货助手.exe'),
            'zip_sha256':sha(target),'files':manifest}
    (release/'manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps({'zip':str(target),'source_commit':commit,'files':len(manifest),'exe_sha256':result['exe_sha256'],
                      'zip_sha256':result['zip_sha256']},ensure_ascii=False))

if __name__=='__main__':main()
