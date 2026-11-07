"""Serial fixed-input performance matrix and separate full-content audits."""
import argparse,hashlib,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/deletion_0462_final'
CASES=[('derived-warm','derived',3,1,'derived_warm'),('native-warm','native',3,1,'native_warm'),
       ('derived-cold','derived',2,1,None),('native-cold','native',2,1,None),
       ('derived-multiple','derived',2,3,'derived_warm'),('native-multiple','native',2,3,'native_warm'),
       ('small','small',3,1,None)]
def sha(path):
    with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def run(cmd,log):
    with log.open('x',encoding='utf-8') as f:
        process=subprocess.Popen(cmd,cwd=ROOT,stdout=f,stderr=f,env={**os.environ,'PYTHONIOENCODING':'utf-8','PYTHONHASHSEED':'0'})
        code=process.wait()
    if code:raise subprocess.CalledProcessError(code,cmd)
def main():
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['measure','audit']);a=p.parse_args()
    inputs=json.loads((QA/'inputs.json').read_text('utf-8'))
    if a.mode=='measure':
        paths=list((ROOT/'src').rglob('*.py'))+list((ROOT/'src').rglob('*.json'))+[ROOT/'tools'/n for n in ('verify_deletion_load.py','deletion_qa.py')]
        environment={'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                     'python':sys.version,'measured_sources':{str(f.relative_to(ROOT)):sha(f) for f in paths}}
        with (QA/'environment.json').open('x',encoding='utf-8') as f:json.dump(environment,f,ensure_ascii=False,indent=2)
        for name,fixture,rounds,parents,warm in CASES:
            cmd=[sys.executable,str(ROOT/'tools/verify_deletion_load.py'),'run','--qa-root',str(QA),'--fixture',inputs[fixture],
                 '--output',str(QA/name),'--rounds',str(rounds),'--parents',str(parents)]
            if warm:cmd+=['--warm',inputs[warm]]
            print('Measuring '+name,flush=True);run(cmd,QA/(name+'-console.txt'))
            report=json.loads((QA/name/'report.json').read_text('utf-8'))
            print(json.dumps({'case':name,'passed':report['passed'],'seconds':{s:v['total_seconds']['median'] for s,v in report['summary'].items()}},ensure_ascii=False),flush=True)
    else:
        for name,*_ in CASES:
            print('Auditing '+name,flush=True)
            run([sys.executable,str(ROOT/'tools/audit_deletion_0462.py'),'--settings',str(QA/name/'after-1/settings'),
                 '--output',str(QA/name/'audit.json')],QA/(name+'-audit.txt'))
        print('All seven full-content audits passed',flush=True)
if __name__=='__main__':main()
