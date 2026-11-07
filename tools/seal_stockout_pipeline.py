"""Seal 0.47.1 isolated acceptance and independently verify a new ZIP."""
import argparse,hashlib,json,re,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
QA=ROOT/'qa/stockout_pipeline_0471'
BASELINE='14f4aa7f96037ab98c2752248f6322cc037976b6'


def read(p):return json.loads(p.read_text('utf-8-sig'))
def sha(p):
    with p.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
def save(p,value):p.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n','utf-8')


def freeze():
    target=QA/'frozen.json'
    if target.exists():raise FileExistsError(target)
    prior=read(ROOT/'releases/0.47.0/rc2/frozen.json')
    protected=dict(prior['protected'])
    manifest=read(ROOT/'releases/0.47.0/rc2/manifest.json')
    protected[str(ROOT/'交付_0.47.0_rc2/补货助手/补货助手.exe')]=manifest['exe_sha256']
    protected[str(ROOT/'交付_0.47.0_rc2/补货助手_0.47.0_rc2_门店缺货率分析.zip')]=manifest['zip_sha256']
    core=dict(prior['core'])
    for name in ['stockout_metrics.py','warehouse_routing.py','virtual_categories.py','aoxiang_acquisition.py']:
        raw=subprocess.check_output(['git','show',BASELINE+':src/'+name],cwd=ROOT)
        core['src/'+name]=hashlib.sha256(raw).hexdigest()
    assert all(sha(ROOT/p)==h for p,h in core.items())
    assert all(sha(Path(p))==h for p,h in protected.items())
    save(target,{'baseline':BASELINE,'core':core,'protected':protected})


def seal(build_id):
    build=read(ROOT/f'build_0.47.1_{build_id}/build_info.json');delivery=ROOT/f'交付_0.47.1_{build_id}/补货助手'
    assert build['version']=='0.47.1'
    assert not subprocess.check_output(['git','diff',build['source_commit'],'--','src','tests','tools','components','prepare_delivery.py'],cwd=ROOT)
    assert not subprocess.check_output(['git','diff',BASELINE,'--','components'],cwd=ROOT)
    frozen=read(QA/'frozen.json')
    assert all(sha(ROOT/p)==h for p,h in frozen['core'].items())
    assert all(sha(Path(p))==h for p,h in frozen['protected'].items())
    regression=(QA/'regression-final.txt').read_text('utf-8-sig')
    count=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$',regression)
    assert count and int(count[1])>=1118
    assert re.search(r'Ran 73 tests .*\s+OK\s*$',(QA/'focused-04.txt').read_text('utf-8-sig'))
    native=(QA/'aoxiang-tests.txt').read_text('utf-8-sig');assert 'pass 175' in native and 'fail 0' in native
    performance=read(QA/'comparison-02/report.json')
    assert performance['passed'] and performance['candidate_file_sha256']==sha(ROOT/'src/stockout_acquisition.py')
    for name in ['source-ui-02','native-header-check']:
        assert read(QA/name/'report.json')['passed']
    exe=read(QA/('exe-'+build_id)/'report.json');original=read(QA/'original-deletion-exe/deletion-report.json')
    for report in [exe,original]:
        assert report['passed'] and report['frozen_exe'] and report['process_exit']==0
        assert report['source_commit']==build['source_commit'] and report['exe_sha256']==sha(delivery/'补货助手.exe')
    assert len(exe['checks'])>=27 and len(exe['pipeline_scopes'])==1 and len(exe['pipeline_scopes'][0]['warehouse_codes'])==2
    release=ROOT/f'releases/0.47.1/{build_id}';release.mkdir(parents=True,exist_ok=False)
    copies={'frozen.json':QA/'frozen.json','regression.txt':QA/'regression-final.txt','focused-tests.txt':QA/'focused-04.txt',
            'aoxiang-tests.txt':QA/'aoxiang-tests.txt','source-ui.json':QA/'source-ui-02/report.json',
            'exe-report.json':QA/('exe-'+build_id)/'report.json','original-deletion-exe.json':QA/'original-deletion-exe/deletion-report.json',
            'performance.json':QA/'comparison-02/report.json','native-header-check.json':QA/'native-header-check/report.json',
            'build_info.json':ROOT/f'build_0.47.1_{build_id}/build_info.json',
            'retained-failures/focused-01.txt':QA/'focused-01.txt','retained-failures/focused-03.txt':QA/'focused-03.txt',
            'retained-failures/source-ui-01.json':QA/'source-ui-01/report.json','retained-failures/source-ui-01-app.txt':QA/'source-ui-01/settings/app.log'}
    for name,source in copies.items():
        target=release/name;target.parent.mkdir(parents=True,exist_ok=True)
        raw=source.read_text('utf-8-sig').rstrip('\n')+'\n';target.write_text(raw,'utf-8',newline='\n')
    for p in (QA/('exe-'+build_id)).glob('*.png'):shutil.copyfile(p,release/p.name)
    files={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    target=delivery.parent/f'补货助手_0.47.1_{build_id}_缺货率并行导出.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as z:
        for name in files:z.write(delivery/name,'补货助手/'+name)
    with zipfile.ZipFile(target) as z:
        assert len(z.namelist())==len(files)
        for name,digest in files.items():assert hashlib.sha256(z.read('补货助手/'+name)).hexdigest()==digest
    save(release/'manifest.json',{'version':'0.47.1','build_id':build_id,'source_commit':build['source_commit'],
         'exe_sha256':sha(delivery/'补货助手.exe'),'zip_sha256':sha(target),'files':files,'regression_count':int(count[1])})
    print(json.dumps({'release':str(release),'zip':str(target),'source_commit':build['source_commit']},ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['freeze','seal']);p.add_argument('--build-id',default='rc1')
    a=p.parse_args();freeze() if a.action=='freeze' else seal(a.build_id)
