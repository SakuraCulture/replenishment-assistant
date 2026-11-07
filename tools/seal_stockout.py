"""Seal reviewed, immutable 0.47.0 evidence and create a fresh independently verified ZIP."""
import argparse,hashlib,json,re,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
QA=ROOT/'qa/stockout_0470'


def read(p):return json.loads(p.read_text('utf-8-sig'))
def sha(p):
    with p.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def freeze():
    target=QA/'frozen.json'
    if target.exists():raise FileExistsError(target)
    core=['src/'+name for name in ('model.py','allocation.py','engine.py','one_box_exception.py','default_config.json','default_must_sell.json','normal_exports.py','jst_orders.py','data_maintenance.py','maintenance_storage.py')]
    assert not subprocess.check_output(['git','diff','e712b4c','--',*core],cwd=ROOT)
    protected=[]
    for name in ('0.45.0','0.45.1','0.46.5'):
        folder=ROOT/f'交付_{name}_rc1';protected.append(folder/'补货助手/补货助手.exe');protected.extend(sorted(folder.glob('*.zip')))
    target.write_text(json.dumps({'baseline':subprocess.check_output(['git','rev-parse','e712b4c'],cwd=ROOT,text=True).strip(),
        'core':{name:sha(ROOT/name) for name in core},'protected':{str(p):sha(p) for p in protected}},ensure_ascii=False,indent=2),'utf-8')


def seal(build_id):
    build=read(ROOT/f'build_0.47.0_{build_id}/build_info.json');delivery=ROOT/f'交付_0.47.0_{build_id}/补货助手'
    assert build['version']=='0.47.0'
    assert not subprocess.check_output(['git','diff',build['source_commit'],'--','src','tests','tools','components','prepare_delivery.py'],cwd=ROOT)
    frozen=read(QA/'frozen.json')
    assert all(sha(ROOT/p)==h for p,h in frozen['core'].items())
    assert all(sha(Path(p))==h for p,h in frozen['protected'].items())
    regression=(QA/'regression-final.txt').read_text('utf-8-sig');count=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$',regression)
    assert count and int(count[1])>=1101
    for name,minimum in [('jst-tests-final.txt',47),('aoxiang-tests-01.txt',175)]:
        text=(QA/name).read_text('utf-8-sig');assert re.search(r'pass '+str(minimum)+r'\b',text) and re.search(r'fail 0\b',text)
    for name in ('source-ui-05','capacity-02','idle-01'):
        assert read(QA/name/'report.json')['passed']
    exe=read(QA/('exe-'+build_id)/'report.json')
    assert exe['passed'] and exe['frozen_exe'] and exe['process_exit']==0 and exe['version']=='0.47.0'
    assert exe['source_commit']==build['source_commit'] and exe['exe_sha256']==sha(delivery/'补货助手.exe')
    release=ROOT/f'releases/0.47.0/{build_id}';release.mkdir(parents=True,exist_ok=False)
    for source,target in [(QA/'frozen.json','frozen.json'),(QA/'regression-final.txt','regression.txt'),
        (QA/'focused-final-01.txt','focused-tests.txt'),(QA/'jst-tests-final.txt','jst-tests.txt'),(QA/'aoxiang-tests-01.txt','aoxiang-tests.txt'),
        (QA/'source-ui-05/report.json','source-ui.json'),(QA/('exe-'+build_id)/'report.json','exe-report.json'),
        (QA/'capacity-01/report.json','capacity-before.json'),(QA/'capacity-02/report.json','capacity-after.json'),
        (QA/'idle-01/report.json','idle-comparison.json'),(ROOT/f'build_0.47.0_{build_id}/build_info.json','build_info.json')]:shutil.copyfile(source,release/target)
    failures=release/'retained-failures';failures.mkdir()
    for name in ('source-ui-01','source-ui-final','exe-rc1'):
        shutil.copyfile(QA/name/'report.json',failures/(name+'.json'))
        shutil.copyfile(QA/name/'settings/app.log',failures/(name+'-app.txt'))
    for p in (QA/('exe-'+build_id)).glob('*.png'):shutil.copyfile(p,release/p.name)
    files={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    target=delivery.parent/f'补货助手_0.47.0_{build_id}_门店缺货率分析.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as z:
        for name in files:z.write(delivery/name,'补货助手/'+name)
    with zipfile.ZipFile(target) as z:
        assert len(z.namelist())==len(files)
        for name,h in files.items():assert hashlib.sha256(z.read('补货助手/'+name)).hexdigest()==h
    manifest={'version':'0.47.0','build_id':build_id,'source_commit':build['source_commit'],'exe_sha256':sha(delivery/'补货助手.exe'),
              'zip_sha256':sha(target),'files':files,'regression_count':int(count[1]),'regression_seconds':float(count[2])}
    (release/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps({'release':str(release),'zip':str(target),'source_commit':build['source_commit'],'regression_count':int(count[1])},ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['freeze','seal']);p.add_argument('--build-id',default='rc2');a=p.parse_args()
    freeze() if a.action=='freeze' else seal(a.build_id)
