"""Freeze previous deliveries and seal the independently validated 0.47.2 build."""
import argparse,hashlib,json,re,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/startup_connections_0472'
BASELINE='c60c32f'


def read(path):return json.loads(path.read_text('utf-8-sig'))
def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n','utf-8',newline='\n')


def freeze():
    path=QA/'frozen.json'
    if path.exists():raise FileExistsError(path)
    previous=read(ROOT/'releases/0.47.1/rc1/frozen.json');manifest=read(ROOT/'releases/0.47.1/rc1/manifest.json')
    protected=dict(previous['protected']);core=dict(previous['core'])
    protected[str(ROOT/'交付_0.47.1_rc1/补货助手/补货助手.exe')]=manifest['exe_sha256']
    protected[str(ROOT/'交付_0.47.1_rc1/补货助手_0.47.1_rc1_缺货率并行导出.zip')]=manifest['zip_sha256']
    assert all(sha(ROOT/p)==h for p,h in core.items())
    assert all(sha(Path(p))==h for p,h in protected.items())
    write(path,{'baseline':subprocess.check_output(['git','rev-parse',BASELINE],cwd=ROOT,text=True).strip(),'core':core,'protected':protected})


def seal(build_id):
    build=read(ROOT/f'build_0.47.2_{build_id}/build_info.json');delivery=ROOT/f'交付_0.47.2_{build_id}/补货助手'
    assert build['version']=='0.47.2'
    assert not subprocess.check_output(['git','diff',build['source_commit'],'--','src','tests','tools','components','prepare_delivery.py'],cwd=ROOT)
    frozen=read(QA/'frozen.json')
    assert all(sha(ROOT/p)==h for p,h in frozen['core'].items())
    assert all(sha(Path(p))==h for p,h in frozen['protected'].items())
    regression=(QA/'regression-final.txt').read_text('utf-8-sig');count=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$',regression)
    assert count and int(count[1])>=1134
    assert re.search(r'Ran 37 tests .*\s+OK\s*$',(QA/'focused-03.txt').read_text('utf-8-sig'))
    native=(QA/'components-03.txt').read_text('utf-8-sig');assert 'pass 229' in native and 'fail 0' in native
    assert read(QA/'source-ui-02/report.json')['passed']
    reports={'startup-exe.json':QA/('exe-'+build_id)/'report.json','analysis-exe.json':QA/'analysis-exe/report.json',
             'original-deletion-exe.json':QA/'original-deletion-exe/deletion-report.json'}
    for source in reports.values():
        report=read(source)
        assert report['passed'] and report['frozen_exe'] and report['process_exit']==0
        assert report['source_commit']==build['source_commit'] and report['exe_sha256']==sha(delivery/'补货助手.exe')
    edge=read(QA/'edge-bundle/report.json')
    assert edge['passed'] and edge['closed']==[True,True] and len(edge['warehouses'])==2
    assert Path(edge['components']).resolve()==(delivery/'_internal/components').resolve()
    release=ROOT/f'releases/0.47.2/{build_id}';release.mkdir(parents=True,exist_ok=False)
    files={'frozen.json':QA/'frozen.json','regression.txt':QA/'regression-final.txt','focused-tests.txt':QA/'focused-03.txt',
           'components-tests.txt':QA/'components-03.txt','source-ui.json':QA/'source-ui-02/report.json',
           'edge-bundle.json':QA/'edge-bundle/report.json','build_info.json':ROOT/f'build_0.47.2_{build_id}/build_info.json',**reports}
    for name,source in files.items():(release/name).write_text(source.read_text('utf-8-sig').rstrip('\n')+'\n','utf-8',newline='\n')
    for source in (QA/('exe-'+build_id)).glob('*.png'):shutil.copyfile(source,release/source.name)
    hashes={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    target=delivery.parent/f'补货助手_0.47.2_{build_id}_启动自动连接.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as archive:
        for name in hashes:archive.write(delivery/name,'补货助手/'+name)
    with zipfile.ZipFile(target) as archive:
        assert len(archive.namelist())==len(hashes)
        for name,digest in hashes.items():assert hashlib.sha256(archive.read('补货助手/'+name)).hexdigest()==digest
    write(release/'manifest.json',{'version':'0.47.2','source_commit':build['source_commit'],'build_id':build_id,
          'exe_sha256':sha(delivery/'补货助手.exe'),'zip_sha256':sha(target),'files':hashes,'regression_count':int(count[1])})
    print(json.dumps({'release':str(release),'zip':str(target)},ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['freeze','seal']);p.add_argument('--build-id',default='rc1');a=p.parse_args()
    freeze() if a.action=='freeze' else seal(a.build_id)
