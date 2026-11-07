"""Seal 0.47.6 negative-stock and status-exclusion acceptance; preserve old builds."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import zipfile

ROOT=Path(__file__).resolve().parents[1]
QA=ROOT/'qa/stockout_negative_0476'
BASELINE='cd69b73faf0af5a262ed9875ecf0cf3e41619370'

def read(path):return json.loads(path.read_text('utf-8-sig'))
def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n','utf-8',newline='\n')
def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def freeze():
    target=QA/'frozen.json'
    if target.exists():raise FileExistsError(target)
    old=read(ROOT/'releases/0.47.5/rc2/frozen.json');manifest=read(ROOT/'releases/0.47.5/rc2/manifest.json')
    protected=dict(old['protected'])
    protected[str(ROOT/'交付_0.47.5_rc2/补货助手/补货助手.exe')]=manifest['exe_sha256']
    protected[str(ROOT/'交付_0.47.5_rc2/补货助手_0.47.5_rc2_全部初始化与勾选采购.zip')]=manifest['zip_sha256']
    assert all(sha(ROOT/p)==h for p,h in old['core'].items())
    assert all(sha(Path(p))==h for p,h in protected.items())
    write(target,{'baseline':BASELINE,'core':old['core'],'protected':protected,
          'scope':'Store stockout only: floor available stock at zero, retain raw values, keep default stopped/disabled exclusions; immutable old snapshots recalculate offline.'})

def seal(build_id):
    build=read(ROOT/f'build_0.47.6_{build_id}/build_info.json')
    delivery=ROOT/f'交付_0.47.6_{build_id}/补货助手'
    assert build['version']=='0.47.6'
    assert not subprocess.check_output(['git','diff',build['source_commit'],'--','src','tests','tools','components','prepare_delivery.py'],cwd=ROOT)
    assert not subprocess.check_output(['git','diff',BASELINE,'--','components'],cwd=ROOT)
    frozen=read(QA/'frozen.json')
    assert all(sha(ROOT/p)==h for p,h in frozen['core'].items())
    assert all(sha(Path(p))==h for p,h in frozen['protected'].items())
    regression=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$',(QA/'regression-final.txt').read_text('utf-8-sig'))
    focused=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$',(QA/'focused-final.txt').read_text('utf-8-sig'))
    assert regression and int(regression[1])>=1194
    assert focused and int(focused[1])>=83
    source_ui=read(QA/'source-ui-final/report.json');exe=read(QA/('exe-'+build_id)/'report.json')
    for value in (source_ui,exe):
        assert value['passed'] and value['version']=='0.47.6' and value['process_exit']==0 and len(value['checks'])>=46
    assert exe['frozen_exe'] and exe['source_commit']==build['source_commit'] and exe['exe_sha256']==sha(delivery/'补货助手.exe')
    real=read(QA/'real-copy-01/report.json')
    assert real['passed'] and real['source_unchanged'] and real['old_snapshots_unchanged'] and real['settings_unchanged']
    release=ROOT/f'releases/0.47.6/{build_id}';release.mkdir(parents=True,exist_ok=False)
    for name,source in {'frozen.json':QA/'frozen.json','regression.txt':QA/'regression-final.txt',
                        'focused-tests.txt':QA/'focused-final.txt','source-ui.json':QA/'source-ui-final/report.json',
                        'analysis-exe.json':QA/('exe-'+build_id)/'report.json',
                        'build_info.json':ROOT/f'build_0.47.6_{build_id}/build_info.json'}.items():
        (release/name).write_text(source.read_text('utf-8-sig').rstrip('\n')+'\n','utf-8',newline='\n')
    # Keep actual store names and local data paths only in ignored local QA.
    sample={k:real[k] for k in ('passed','source_database_sha256','source_unchanged','old_snapshots_unchanged',
                               'settings_unchanged','seconds','copied_originals','source_snapshots','scope')}
    sample.update(before_all=real['before']['total'],after_all=real['after']['total'],
                  before_default=real['before_default_status_exclusions']['total'],
                  after_default=real['after_default_status_exclusions']['total'],
                  recalculation_warning_before=real['before']['needs_negative_stock_recalculation'],
                  recalculation_warning_after=real['after']['needs_negative_stock_recalculation'])
    write(release/'real-sample.json',sample)
    for path in (QA/('exe-'+build_id)).glob('*.png'):shutil.copyfile(path,release/path.name)
    hashes={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    target=delivery.parent/f'补货助手_0.47.6_{build_id}_负库存计入缺货率.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as archive:
        for name in hashes:archive.write(delivery/name,'补货助手/'+name)
    with zipfile.ZipFile(target) as archive:
        assert len(archive.namelist())==len(hashes)
        for name,digest in hashes.items():assert hashlib.sha256(archive.read('补货助手/'+name)).hexdigest()==digest
    write(release/'manifest.json',{'version':'0.47.6','source_commit':build['source_commit'],'build_id':build_id,
          'exe_sha256':sha(delivery/'补货助手.exe'),'zip_sha256':sha(target),'files':hashes,
          'regression_count':int(regression[1]),'focused_count':int(focused[1]),'exe_check_count':len(exe['checks'])})
    print(json.dumps({'release':str(release),'zip':str(target)},ensure_ascii=False))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['freeze','seal']);parser.add_argument('--build-id',default='rc1')
    args=parser.parse_args();freeze() if args.action=='freeze' else seal(args.build_id)
