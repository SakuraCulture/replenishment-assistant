"""Run the real signing dialog/SQLite contention in isolated source or frozen UI."""
import argparse,hashlib,json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--exe',type=Path);p.add_argument('--fixture',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    env={**os.environ,'REPLENISHMENT_DATA_DIR':str(out/'settings'),'PYTHONIOENCODING':'utf-8','PYTHONFAULTHANDLER':'1'};env.pop('QT_QPA_PLATFORM',None)
    cmd=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    start=time.perf_counter()
    with (out/'process.txt').open('w',encoding='utf-8') as log:
        result=subprocess.run(cmd+['--gui-maintenance-smoke',str(a.fixture.resolve()),str(out)],cwd=ROOT,env=env,stdout=log,stderr=log,timeout=240)
    (out/'exit.json').write_text(json.dumps({'process_exit':result.returncode,'seconds':time.perf_counter()-start}), 'utf-8')
    report=json.loads((out/'gui-report.json').read_text('utf-8'));report.update(process_exit=result.returncode,seconds=time.perf_counter()-start)
    if a.exe:report['exe_sha256']=hashlib.sha256(a.exe.read_bytes()).hexdigest()
    (out/'gui-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps(report,ensure_ascii=False))
    if result.returncode or not report['passed'] or a.exe and not report['frozen_exe']:raise AssertionError('UI acceptance failed')

if __name__=='__main__':main()
