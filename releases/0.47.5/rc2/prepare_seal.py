"""Collect exact successful reports into a fresh seal input, retaining failures.

No original run directory is overwritten. The stock sealer reads the collected
names; this manifest records their actual source paths and hashes.
"""
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
ROOT=next(p for p in Path(__file__).resolve().parents if (p/'src/app.py').is_file() and (p/'.git').exists())
QA=ROOT/'qa/reset_selected_0475'
TARGET=QA/'seal-rc2'

def sha(path):
    with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def main():
    log=(QA/'regression-02.txt').read_text('utf-8-sig')
    assert re.search(r'Ran 1190 tests in [\d.]+s\s+OK\s*$',log)
    TARGET.mkdir(exist_ok=False)
    copies={
        'frozen.json':'frozen.json',
        'regression-final.txt':'regression-02.txt',
        'focused-final.txt':'focused-02.txt',
        'regression-01.txt':'regression-01.txt',
        'source-ui-01/report.json':'source-ui-01/report.json',
        'source-ui-rc2/report.json':'source-ui-rc2/report.json',
        'source-ui-rc2/restart-report.json':'source-ui-rc2/restart-report.json',
        'reset-exe-rc2/report.json':'reset-exe-rc2/report.json',
        'reset-exe-rc2/restart-report.json':'reset-exe-rc2/restart-report.json',
        'startup-exe-rc2/report.json':'startup-exe-rc2/report.json',
        'deletion-exe-rc2/deletion-report.json':'deletion-exe-rc2-valid/deletion-report.json',
        'exe-rc2/report.json':'exe-rc2/report.json',
    }
    for p in (QA/'reset-exe-rc2').glob('*.png'):copies['reset-exe-rc2/'+p.name]='reset-exe-rc2/'+p.name
    manifest={}
    for target,source in copies.items():
        src=QA/source;dst=TARGET/target;dst.parent.mkdir(exist_ok=True,parents=True);shutil.copyfile(src,dst)
        assert sha(src)==sha(dst)
        manifest[target]={'source':str(src.relative_to(ROOT)),'sha256':sha(src)}
    sys.path.insert(0,str(ROOT/'tools'))
    import seal_reset_selected as seal
    seal.QA=TARGET;seal.seal('rc2')
    release=ROOT/'releases/0.47.5/rc2'
    (release/'acceptance-sources.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n','utf-8')
    shutil.copyfile(QA/'deletion-exe-rc2/deletion-report.json',release/'first-deletion-exe-wrong-fixture.json')
    shutil.copyfile(ROOT/'build_0.47.5_rc1/build_info.json',release/'not-released-rc1-build.json')
    shutil.copyfile(Path(__file__),release/'prepare_seal.py')

if __name__=='__main__':main()
