"""Route retained attempts explicitly without changing the committed sealer gates."""
import difflib,hashlib,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
QA=ROOT/'qa/deletion_0462_final'
original=(ROOT/'tools/seal_deletion_0462.py').read_text('utf-8')
text=original
def replace(old,new):
    global text
    assert text.count(old)==1,old
    text=text.replace(old,new)
replace("from run_deletion_0462 import CASES\nROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/deletion_0462_final';PRIOR=ROOT/'qa/deletion_0462'",
        "import sys\nROOT=Path(__file__).resolve().parents[2]\nsys.path.insert(0,str(ROOT/'tools'))\nfrom run_deletion_0462 import CASES\nQA=ROOT/'qa/deletion_0462_final';PRIOR=ROOT/'qa/deletion_0462'")
replace("backup=read(QA/'exe-storage/storage_ui_report.json');recovery=read(QA/'exe-recovery/process-acceptance.json')",
        "backup=read(QA/'exe-storage-r2/storage_ui_report.json');recovery=read(QA/'exe-recovery-r2/process-acceptance.json')")
replace("    release.mkdir(parents=True,exist_ok=False)","""    first_storage=read(QA/'exe-storage/storage_ui_report.json')
    first_recovery=read(QA/'exe-recovery/process-acceptance.json')
    assert not first_storage['passed'] and not first_storage['checks'] and not first_storage['remaining_children']
    assert not first_recovery['accepted'] and not first_recovery['checks'] and first_recovery['shared_component_closed']
    fixture=read(QA/'storage-input-record.json');assert sha(Path(fixture['fixture']))==fixture['sha256']
    release.mkdir(parents=True,exist_ok=False)""")
replace("    for folder,report in [('exe-deletion','deletion-report.json'),('exe-storage','storage_ui_report.json'),('exe-recovery','process-acceptance.json')]:\n        shutil.copyfile(QA/folder/report,release/(folder+'.json'));dest=release/folder;dest.mkdir()",
        "    for folder,report,name in [('exe-deletion','deletion-report.json','exe-deletion'),('exe-storage-r2','storage_ui_report.json','exe-storage'),('exe-recovery-r2','process-acceptance.json','exe-recovery')]:\n        shutil.copyfile(QA/folder/report,release/(name+'.json'));dest=release/name;dest.mkdir()")
replace("    pilots=release/'exploratory';pilots.mkdir()", """    pilots=release/'exploratory';pilots.mkdir()
    for source,name in [
        ('exe-storage/storage_ui_report.json','storage-first-input-failure.json'),
        ('exe-storage-console.txt','storage-first-input-failure.txt'),
        ('exe-recovery/process-acceptance.json','recovery-first-startup-timeout.json'),
        ('exe-recovery-console.txt','recovery-first-startup-timeout.txt'),
        ('component-startup-probe/probe.json','component-startup-probe.json'),
    ]:shutil.copyfile(QA/source,pilots/name)
    for name in ('storage-input-record.json','sealer-output-routing.patch','seal_delivery.py',
                 'prepare_seal.py','run_recovery_without_kill.py','run_recovery_r2_without_kill.py',
                 'probe_component_startup.py','exe-deletion-console.txt','exe-storage-r2-console.txt',
                 'exe-recovery-r2-console.txt'):
        shutil.copyfile(QA/name,release/name)""")
(QA/'seal_delivery.py').write_text(text,'utf-8')
(QA/'sealer-output-routing.patch').write_text(''.join(difflib.unified_diff(original.splitlines(True),text.splitlines(True),fromfile='tools/seal_deletion_0462.py',tofile='qa/deletion_0462_final/seal_delivery.py')),'utf-8')
print('Prepared explicit r2 output routing; all committed release gates preserved.')
