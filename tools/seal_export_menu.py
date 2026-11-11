"""Seal 0.49.5 using this run's independent evidence and immutable build."""
import json,re,subprocess,zipfile,shutil
from pathlib import Path
from seal_payment_reference import sha,read,write,tests
from aoxiang_bundle import verify
ROOT=Path(__file__).resolve().parents[1]
QA=ROOT/'qa/export_recovery_0495_20261109'
def main():
    build=read(ROOT/'build_0.49.5_rc2/build_info.json');commit=build['source_commit']
    assert build['version']=='0.49.5'
    assert not subprocess.check_output(['git','diff',commit,'--','src','components','tools','tests'],cwd=ROOT)
    regression=tests(QA/'regression-final.log',1335)
    node=(QA/'node-final.log').read_text('utf-8');assert re.search(r'pass 204\b',node) and re.search(r'fail 0\b',node)
    source=read(QA/'source2/process-acceptance.json');exe=read(QA/'exe2/process-acceptance.json');pack=read(QA/'packaged2/acceptance.json')
    assert all(r['passed'] and not r['live_platform'] for r in (source,exe,pack))
    assert exe['accepted'] and exe['process_exit']==0 and exe['shutdown_complete']
    assert exe['frozen'] and exe['source_commit']==commit and len(exe['checks'])==5
    protected=read(QA/'preserved.json')
    for group in protected.values():
        assert all(sha(ROOT/p)==h for p,h in group.items())
    delivery=ROOT/'交付_0.49.5_rc2/补货助手';component=verify(delivery/'_internal/components/aoxiang')
    for p in (ROOT/'build_0.49.5_rc2/source/components/aoxiang/src').glob('*.mjs'):
        assert sha(p)==sha(delivery/'_internal/components/aoxiang/src'/p.name)
    shutil.copy2(ROOT/'build_0.49.5_rc2/source/docs/0.49.5翱象导出菜单与待核恢复.md',delivery/'0.49.5翱象导出菜单与待核恢复.md')
    target=delivery.parent/'补货助手_0.49.5_rc2.zip';assert not target.exists()
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for p in sorted(delivery.rglob('*')):
            if p.is_file():z.write(p,p.relative_to(delivery.parent))
    with zipfile.ZipFile(target) as z:
        assert z.testzip() is None
        assert len(z.namelist())==sum(p.is_file() for p in delivery.rglob('*'))
    release=ROOT/'releases/0.49.5/rc2';release.mkdir(parents=True,exist_ok=False)
    shutil.copy2(QA/'exe2/恢复按钮.png',release/'恢复按钮.png')
    shutil.copy2(QA/'regression-final.log',release/'regression.log');shutil.copy2(QA/'node-final.log',release/'node.log')
    for name,folder in [('exe','exe2'),('source','source2'),('packaged','packaged2')]:shutil.copy2(QA/folder/('acceptance.json' if name=='packaged' else 'process-acceptance.json'),release/(name+'.json'))
    result={'version':'0.49.5','source_commit':commit,'regression':regression,'node_tests':204,'exe_checks':5,'packaged_edge_tests':3,
        'core_preserved':len(protected['core']),'old_deliveries_preserved':len(protected['deliveries']),'exe_sha256':sha(delivery/'补货助手.exe'),
        'zip':str(target.relative_to(ROOT)),'zip_sha256':sha(target),'live_platform':False,'remote_records_modified':False}
    write(release/'validation.json',result);shutil.copy2(ROOT/'build_0.49.5_rc2/build_info.json',release/'build_info.json')
    print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
