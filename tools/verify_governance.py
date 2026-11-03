"""Frozen-source, isolated QA for the parameter/admission/plan release."""
import argparse, hashlib, json, os, shutil, subprocess, sys, zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
QA=ROOT/'qa/governance_032'
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def freeze():
    QA.mkdir(exist_ok=False)
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    subprocess.run(['git','archive','--format=zip','-o',str(QA/'baseline.zip'),commit],cwd=ROOT,check=True)
    with zipfile.ZipFile(QA/'baseline.zip') as z:z.extractall(QA/'baseline')
    shutil.copytree(ROOT/'qa/catalog_full_20261103/inputs',QA/'inputs')
    protected={}
    for directory in [ROOT/'交付_0.31.0_rc5',Path(os.environ['LOCALAPPDATA'])/'YoutuanTools/补货助手/settings']:
        for p in directory.rglob('*'):
            if p.is_file():protected[str(p)]=sha(p)
    for p in (QA/'inputs').rglob('*'):
        if p.is_file():protected[str(p)]=sha(p)
    (QA/'baseline.json').write_text(json.dumps({'commit':commit,'protected':protected},ensure_ascii=False,indent=2),'utf-8')
def capture(name,source):
    subprocess.run([sys.executable,str(ROOT/'tools/parallel_qa.py'),'capture','--qa',str(QA),'--source',str(source),'--output',str(QA/name)],check=True,cwd=ROOT,env={**os.environ,'PYTHONHASHSEED':'0','PYTHONIOENCODING':'utf-8'})
def compare(name):
    sys.path.insert(0,str(ROOT/'tools'))
    from parallel_qa import diff,read,clean,WHITELIST
    from structure_oracle import excel_snapshot
    before,after=QA/'before',QA/name
    cases=[]
    for p in before.glob('*.pklz'):cases.append({'name':p.name,'differences':diff(clean(read(p)),clean(read(after/p.name)))})
    for p in before.glob('*.xlsx'):cases.append({'name':p.name,'differences':diff(excel_snapshot(p),excel_snapshot(after/p.name))})
    frozen=json.loads((QA/'baseline.json').read_text('utf-8'))
    changed=[p for p,h in frozen['protected'].items() if not Path(p).is_file() or sha(p)!=h]
    core=['model.py','one_box_exception.py','allocation.py','default_config.json']
    core_changed=[p for p in core if sha(QA/'baseline/src'/p)!=sha(ROOT/'src'/p)]
    report={'cases':cases,'whitelist':WHITELIST,'protected_changed':changed,'core_changed':core_changed}
    report['passed']=bool(cases) and not changed and not core_changed and not any(c['differences'] for c in cases)
    (QA/(name+'-equivalence.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps({'passed':report['passed'],'cases':len(cases),'differences':sum(len(c['differences']) for c in cases),'protected_changed':len(changed)}))
    if not report['passed']:raise SystemExit(1)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['freeze','baseline','capture','compare']);p.add_argument('--name',default='after');a=p.parse_args()
    if a.action=='freeze':freeze()
    elif a.action=='baseline':capture('before',QA/'baseline')
    elif a.action=='capture':capture(a.name,ROOT)
    else:compare(a.name)
