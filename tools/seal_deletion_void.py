"""Seal this version's isolated evidence and immutable build; never overwrite a release."""
import argparse,hashlib,json,re,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
QA=ROOT/'qa/deletion_void_0464'
BASE='6456aaeb21bca2d58e1fb3cfb216fcfdc8cf7c8a'


def read(path):return json.loads(path.read_text('utf-8'))
def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
def git(*args):return subprocess.check_output(['git',*args],cwd=ROOT)
def write(path,value):
    with path.open('x',encoding='utf-8') as stream:json.dump(value,stream,ensure_ascii=False,indent=2)


def capture():
    paths=sorted((ROOT/'qa/deletion_0321/inputs').rglob('*'))
    for version in ('0.45.0','0.45.1','0.46.2','0.46.3'):
        folder=ROOT/f'交付_{version}_rc1';exe=folder/'补货助手/补货助手.exe';archives=sorted(folder.glob('*.zip'))
        assert exe.is_file() and archives,version
        paths += [exe,*archives]
    values={p.relative_to(ROOT).as_posix():sha(p) for p in paths if p.is_file()}
    assert len(values)>=12 and '交付_0.46.3_rc1/补货助手/补货助手.exe' in values
    write(QA/'protected-before.json',values);print(json.dumps({'captured_files':len(values)}))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--capture',action='store_true');args=parser.parse_args()
    if args.capture:return capture()
    build=read(ROOT/'build_0.46.4_rc1/build_info.json');commit=build['source_commit']
    delivery=ROOT/'交付_0.46.4_rc1/补货助手';release=ROOT/'releases/0.46.4/rc1'
    assert build['version']=='0.46.4' and not git('diff',commit,'--','src','components','tools','tests','prepare_delivery.py')
    assert sha(delivery/'使用说明.txt')==sha(ROOT/'docs/0.46.4作废采购核对与删除恢复.md')
    log=(QA/'regression-r2.log').read_text('utf-8-sig')
    passed=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$',log)
    assert passed and int(passed[1])>=1063
    node=(QA/'node-r3.log').read_text('utf-8-sig')
    assert re.search(r'pass 175\b',node) and re.search(r'fail 0\b',node)
    for name,count in [('deletion-focused-r1.log',13),('recovery-focused-r1.log',16)]:
        assert re.search(r'Ran '+str(count)+r' tests in [\d.]+s\s+OK\s*$',(QA/name).read_text('utf-8-sig'))
    executable=delivery/'补货助手.exe';exe_hash=sha(executable)
    for folder,count in [('exe-r1',7),('exe-recovery-r1',9)]:
        report=read(QA/f'{folder}/process-acceptance.json')
        assert report['accepted'] and report['frozen_exe'] and report['source_commit']==commit
        assert report['exe_sha256']==exe_hash and len(report['checks'])==count and report['synthetic_uploads']==0
    deletion=read(QA/'exe-delete-r1/deletion-report.json')
    assert deletion['passed'] and deletion['frozen_exe'] and deletion['process_exit']==0
    assert deletion['exe_sha256']==exe_hash and len(deletion['checks'])==8
    protected=read(QA/'protected-before.json')
    assert all(sha(ROOT/p)==h for p,h in protected.items())
    core=['model.py','engine.py','allocation.py','parameter_schema.py','default_config.json','default_must_sell.json',
          'warehouse_catalog.py','data_maintenance.py','maintenance_storage.py','normal_exports.py','jst_orders.py']
    audit={}
    for path in ['src/'+n for n in core]+['components/aoxiang/src/purchase-create-ledger.mjs']:
        before=hashlib.sha256(git('show',BASE+':'+path)).hexdigest();after=sha(ROOT/path)
        assert before==after,path;audit[path]={'baseline_sha256':before,'candidate_sha256':after}
    release.mkdir(parents=True,exist_ok=False)
    copies={'regression-r2.log':'regression.txt','node-r3.log':'node-tests.txt',
        'deletion-focused-r1.log':'deletion-focused.txt','recovery-focused-r1.log':'recovery-focused.txt',
        'source-ui-r2/process-acceptance.json':'source-ui.json','exe-r1/process-acceptance.json':'exe-report.json',
        'exe-recovery-r1/process-acceptance.json':'exe-recovery.json','exe-delete-r1/deletion-report.json':'exe-delete.json'}
    for source,target in copies.items():shutil.copyfile(QA/source,release/target)
    shutil.copyfile(ROOT/'build_0.46.4_rc1/build_info.json',release/'build_info.json')
    failures=release/'retained-failures';failures.mkdir()
    for source,target in [('node-focused-r1.log','node-fixture-r1.txt'),('node-r2.log','node-regression-r2.txt'),
                          ('source-ui-r1/process-acceptance.json','source-ui-r1.json'),('regression-r1.log','regression-r1.txt')]:
        shutil.copyfile(QA/source,failures/target)
    images=release/'exe-screenshots';images.mkdir()
    for p in (QA/'exe-r1').glob('*.png'):shutil.copyfile(p,images/p.name)
    (release/'runtime.patch').write_bytes(git('diff',BASE,commit,'--','src','components/aoxiang/src'))
    write(release/'source-audit.json',{'baseline':BASE,'source_commit':commit,'protected_source':audit,
          'protected_files':protected,'python_tests':int(passed[1]),'python_seconds':float(passed[2]),
          'node_tests':175,'actual_exe_checks':24,'live_platform':False,'performance_remeasured':False})
    manifest={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    target=delivery.parent/'补货助手_0.46.4_rc1_作废采购核对与删除恢复.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as z:
        for name in manifest:z.write(delivery/name,'补货助手/'+name)
    with zipfile.ZipFile(target) as z:
        assert len(z.namelist())==len(manifest)
        for name,digest in manifest.items():assert hashlib.sha256(z.read('补货助手/'+name)).hexdigest()==digest,name
    result={'version':'0.46.4','source_commit':commit,'exe_sha256':exe_hash,'zip_sha256':sha(target),'files':manifest}
    write(release/'manifest.json',result)
    print(json.dumps({'zip':str(target),'source_commit':commit,'files':len(manifest),'exe_sha256':exe_hash,
                      'zip_sha256':result['zip_sha256']},ensure_ascii=False))


if __name__=='__main__':main()
