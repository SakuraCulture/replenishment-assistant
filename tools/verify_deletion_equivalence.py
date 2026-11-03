"""Re-run existing ordered calculation/Excel oracle against fixed 0.32.0."""
import argparse,hashlib,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/deletion_0321'
sys.path.insert(0,str(ROOT/'tools'))
from parallel_qa import read,clean,diff,WHITELIST
from structure_oracle import excel_snapshot
def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1048576),b''):h.update(block)
    return h.hexdigest()
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--name',required=True);parser.add_argument('--capture',action='store_true');args=parser.parse_args()
    before=QA/(args.name+'-before');after=QA/(args.name+'-after')
    if args.capture:
        for source,out in ((QA/'baseline',before),(ROOT,after)):
            subprocess.run([sys.executable,str(ROOT/'tools/parallel_qa.py'),'capture','--qa',str(ROOT/'qa/governance_032'),'--source',str(source),'--output',str(out)],cwd=ROOT,env={**os.environ,'PYTHONHASHSEED':'0','PYTHONIOENCODING':'utf-8'},check=True)
    cases=[]
    for path in before.glob('*.pklz'):cases.append({'name':path.name,'differences':diff(clean(read(path)),clean(read(after/path.name)))})
    for path in before.glob('*.xlsx'):cases.append({'name':path.name,'differences':diff(excel_snapshot(path),excel_snapshot(after/path.name))})
    frozen=json.loads((QA/'baseline.json').read_text('utf-8'))
    protected=[p for p,h in frozen['protected'].items() if sha(p)!=h]
    core=[p for p,h in frozen['core'].items() if sha(ROOT/'src'/p)!=h]
    report={'passed':len(cases)==38 and not protected and not core and not any(c['differences'] for c in cases),'cases':cases,'whitelist':WHITELIST,'core_changed':core,'core_sha256':frozen['core'],'protected_changed':protected,'input_sha256':{str(p):sha(p) for p in (ROOT/'qa/governance_032/inputs').iterdir() if p.is_file()},'PYTHONHASHSEED':'0 for both, preserving strict dictionary insertion order; no sorting in comparison'}
    (QA/(args.name+'-equivalence.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps({'passed':report['passed'],'cases':len(cases),'differences':sum(len(c['differences']) for c in cases)}))
    if not report['passed']:raise SystemExit(1)
if __name__=='__main__':main()
