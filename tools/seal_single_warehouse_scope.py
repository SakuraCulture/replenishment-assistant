"""Seal 0.46.5 after exact comparison, regression and isolated packaged UI QA."""
import hashlib,json,re,shutil,subprocess,zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
QA=ROOT/'qa/single_warehouse_0465'
def read(p):return json.loads(p.read_text('utf-8-sig'))
def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def main():
    release=ROOT/'releases/0.46.5/rc1';delivery=ROOT/'交付_0.46.5_rc1/补货助手'
    frozen=read(QA/'frozen.json');build=read(ROOT/'build_0.46.5_rc1/build_info.json')
    assert build['version']=='0.46.5'
    assert not subprocess.check_output(['git','diff',build['source_commit'],'--','src','tests','tools','components','prepare_delivery.py','verify_warehouse_exe.py'],cwd=ROOT)
    regression=(QA/'regression.txt').read_text('utf-8-sig')
    count=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$',regression)
    assert count and int(count[1])>=1070,'Full regression did not pass'
    comparison=read(QA/'comparison-tags/report.json')
    assert comparison['passed'] and len(comparison['cases'])==48 and not comparison['unexpected_differences']
    for name,digest in comparison['input_hashes'].items():assert sha(QA/'comparison-tags/inputs'/name)==digest,name
    source_ui=read(QA/'source-ui-current/warehouse_ui_report.json')
    exe=read(QA/'exe/warehouse_ui_report.json')
    assert source_ui['passed'] and len(source_ui['checks'])==6
    assert exe['passed'] and len(exe['checks'])==6 and exe['frozen_exe'] and exe['process_exit']==0
    assert exe['version']=='0.46.5' and exe['source_commit']==build['source_commit']
    assert exe['exe_sha256']==sha(delivery/'补货助手.exe')
    core={name:sha(ROOT/name)==digest for name,digest in frozen['core'].items()}
    protected={name:sha(Path(name))==digest for name,digest in frozen['protected'].items()}
    assert all(core.values()) and all(protected.values())
    release.mkdir(parents=True,exist_ok=False)
    for src,name in [(QA/'frozen.json','frozen.json'),(QA/'regression.txt','regression.txt'),
                     (QA/'focused-initial.txt','single-warehouse-tests.txt'),
                     (QA/'comparison-tags/report.json','comparison.json'),
                     (QA/'source-ui-current/warehouse_ui_report.json','source-ui.json'),
                     (QA/'exe/warehouse_ui_report.json','exe-report.json'),
                     (ROOT/'build_0.46.5_rc1/build_info.json','build_info.json')]:
        shutil.copyfile(src,release/name)
    failures=release/'retained-failures';failures.mkdir()
    for folder in ['source-ui','source-ui-confirmations','source-ui-tags','source-ui-filter']:
        shutil.copyfile(QA/folder/'warehouse_ui_report.json',failures/(folder+'.json'))
    shutil.copyfile(QA/'comparison/old.log',failures/'missing-must-sell-profile.txt')
    shutil.copyfile(QA/'comparison-final/report.json',failures/'oracle-audit-snapshot.json')
    for p in (QA/'exe').glob('*.png'):shutil.copyfile(p,release/p.name)
    audit={'baseline':frozen['baseline'],'source_commit':build['source_commit'],
           'core_unchanged':core,'protected_files_unchanged':protected,
           'runtime_diff':subprocess.check_output(['git','diff','--stat',frozen['baseline'],build['source_commit'],'--','src'],cwd=ROOT,text=True)}
    (release/'source-audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
    patch=subprocess.check_output(['git','diff',frozen['baseline'],build['source_commit'],'--','src/warehouse_catalog.py'],cwd=ROOT)
    (release/'routing.patch').write_bytes(patch)
    files={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    target=delivery.parent/'补货助手_0.46.5_rc1_单仓商品归属修正.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as z:
        for name in files:z.write(delivery/name,'补货助手/'+name)
    with zipfile.ZipFile(target) as z:
        assert len(z.namelist())==len(files)
        for name,digest in files.items():assert hashlib.sha256(z.read('补货助手/'+name)).hexdigest()==digest,name
    manifest={'version':'0.46.5','build_id':'rc1','source_commit':build['source_commit'],
              'exe_sha256':sha(delivery/'补货助手.exe'),'zip_sha256':sha(target),'files':files}
    (release/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'zip':str(target),'files':len(files),'sha256':manifest['zip_sha256'],
                      'regression_count':int(count[1]),'regression_seconds':float(count[2])},ensure_ascii=False))


if __name__=='__main__':main()
