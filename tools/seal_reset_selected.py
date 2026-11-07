"""Seal independent 0.47.5 checks and verify old deliveries and new ZIP contents."""
import argparse,hashlib,json,re,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/reset_selected_0475';BASELINE='752947a'


def read(path):return json.loads(path.read_text('utf-8-sig'))
def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n','utf-8',newline='\n')


def freeze():
    target=QA/'frozen.json'
    if target.exists():raise FileExistsError(target)
    old=read(ROOT/'releases/0.47.4/rc1/frozen.json');manifest=read(ROOT/'releases/0.47.4/rc1/manifest.json')
    core=dict(old['core'])
    protected=dict(old['protected'])
    protected[str(ROOT/'交付_0.47.4_rc1/补货助手/补货助手.exe')]=manifest['exe_sha256']
    protected[str(ROOT/'交付_0.47.4_rc1/补货助手_0.47.4_rc1_状态剔除与作业管理.zip')]=manifest['zip_sha256']
    assert all(sha(ROOT/p)==h for p,h in core.items())
    assert all(sha(Path(p))==h for p,h in protected.items())
    write(target,{'baseline':subprocess.check_output(['git','rev-parse',BASELINE],cwd=ROOT,text=True).strip(),
                  'core':core,'protected':protected,'changed_query_module':'Whole local reset after safe shutdown; exact checked purchase scope; stale local order guards; no model or normal deletion changes'})


def seal(build_id):
    build=read(ROOT/f'build_0.47.5_{build_id}/build_info.json');delivery=ROOT/f'交付_0.47.5_{build_id}/补货助手'
    assert build['version']=='0.47.5'
    assert not subprocess.check_output(['git','diff',build['source_commit'],'--','src','tests','tools','components','prepare_delivery.py'],cwd=ROOT)
    assert not subprocess.check_output(['git','diff',BASELINE,'--','components'],cwd=ROOT)
    frozen=read(QA/'frozen.json')
    assert all(sha(ROOT/p)==h for p,h in frozen['core'].items())
    assert all(sha(Path(p))==h for p,h in frozen['protected'].items())
    regression=(QA/'regression-final.txt').read_text('utf-8-sig');count=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$',regression)
    assert count and int(count[1])>=1190
    focused=re.search(r'Ran (\d+) tests in [\d.]+s\s+OK\s*$',(QA/'focused-final.txt').read_text('utf-8-sig'))
    assert focused and int(focused[1])>=95
    source_ui=read(QA/'source-ui-rc2/report.json')
    assert source_ui['passed'] and source_ui['version']=='0.47.5' and len(source_ui['checks'])>=13
    reports={'reset-exe.json':QA/('reset-exe-'+build_id)/'report.json','restart-exe.json':QA/('reset-exe-'+build_id)/'restart-report.json','analysis-exe.json':QA/('exe-'+build_id)/'report.json','startup-exe.json':QA/('startup-exe-'+build_id)/'report.json',
             'deletion-exe.json':QA/('deletion-exe-'+build_id)/'deletion-report.json'}
    for source in reports.values():
        report=read(source)
        assert report['passed'] and report['frozen_exe'] and report['process_exit']==0
        assert report['source_commit']==build['source_commit'] and report['exe_sha256']==sha(delivery/'补货助手.exe')
    assert len(read(reports['reset-exe.json'])['checks'])>=13
    assert len(read(reports['restart-exe.json'])['checks'])>=6
    assert len(read(reports['analysis-exe.json'])['checks'])>=44
    assert len(read(reports['startup-exe.json'])['checks'])>=22
    assert len(read(reports['deletion-exe.json'])['checks'])>=8
    release=ROOT/f'releases/0.47.5/{build_id}';release.mkdir(parents=True,exist_ok=False)
    files={'frozen.json':QA/'frozen.json','regression.txt':QA/'regression-final.txt','focused-tests.txt':QA/'focused-final.txt',
           'source-ui.json':QA/'source-ui-rc2/report.json','source-restart.json':QA/'source-ui-rc2/restart-report.json',
           'first-source-ui.json':QA/'source-ui-01/report.json','first-regression.txt':QA/'regression-01.txt',
           'build_info.json':ROOT/f'build_0.47.5_{build_id}/build_info.json',
           **reports}
    for name,source in files.items():
        target=release/name;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(source.read_text('utf-8-sig').rstrip('\n')+'\n','utf-8',newline='\n')
    for folder in ('reset-exe-'+build_id,):
        for source in (QA/folder).glob('*.png'):shutil.copyfile(source,release/source.name)
    hashes={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    target=delivery.parent/f'补货助手_0.47.5_{build_id}_全部初始化与勾选采购.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as archive:
        for name in hashes:archive.write(delivery/name,'补货助手/'+name)
    with zipfile.ZipFile(target) as archive:
        assert len(archive.namelist())==len(hashes)
        for name,digest in hashes.items():assert hashlib.sha256(archive.read('补货助手/'+name)).hexdigest()==digest
    write(release/'manifest.json',{'version':'0.47.5','source_commit':build['source_commit'],'build_id':build_id,
          'exe_sha256':sha(delivery/'补货助手.exe'),'zip_sha256':sha(target),'files':hashes,'regression_count':int(count[1])})
    print(json.dumps({'release':str(release),'zip':str(target)},ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['freeze','seal']);p.add_argument('--build-id',default='rc2');a=p.parse_args()
    freeze() if a.action=='freeze' else seal(a.build_id)
