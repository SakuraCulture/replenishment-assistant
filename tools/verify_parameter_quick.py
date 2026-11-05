"""Isolated native UI acceptance for the parameter quick actions."""
import argparse,json,os,subprocess,sys,hashlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--exe',type=Path);a=p.parse_args()
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);(out/'synthetic.json').write_text('{"synthetic":true}','utf-8')
    env={**os.environ,'REPLENISHMENT_DATA_DIR':str(out/'settings'),'PYTHONIOENCODING':'utf-8'};env.pop('QT_QPA_PLATFORM',None)
    cmd=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    with (out/'process.txt').open('w',encoding='utf-8') as log:
        run=subprocess.run(cmd+['--parameter-quick-smoke',str(out)],cwd=ROOT,env=env,stdout=log,stderr=log,timeout=180)
    report=json.loads((out/'report.json').read_text('utf-8'));report['exit_code']=run.returncode
    if a.exe:report['exe_sha256']=hashlib.sha256(a.exe.read_bytes()).hexdigest()
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');print(json.dumps(report,ensure_ascii=False))
    if run.returncode or not report['passed'] or a.exe and not report['frozen_exe']:raise AssertionError('UI acceptance failed')
if __name__=='__main__':main()
