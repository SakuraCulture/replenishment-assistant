"""Run isolated source/EXE task recovery UI. Never kill an in-progress task."""
import argparse,hashlib,json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def main():
    p=argparse.ArgumentParser();p.add_argument('--exe',type=Path);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--scenario',choices=['purchase-recovery','deletion-void'],default='purchase-recovery');a=p.parse_args()
    out=a.out.resolve();out.relative_to(ROOT/'qa');out.mkdir(parents=True,exist_ok=False)
    (out/'fixture.json').write_text(json.dumps({'synthetic':True,'live_platform':False}),'utf-8')
    env={**os.environ,'REPLENISHMENT_DATA_DIR':str(out/'settings'),'LOCALAPPDATA':str(out/'localappdata'),'PYTHONUTF8':'1'}
    env.pop('QT_QPA_PLATFORM',None)
    cmd=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    with (out/'process.log').open('w',encoding='utf-8') as log:
        process=subprocess.Popen(cmd+['--'+a.scenario+'-smoke',str(out)],cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
        started=time.monotonic()
        while process.poll() is None:
            if time.monotonic()-started>180:
                (out/'still-running.json').write_text(json.dumps({'pid':process.pid,'not_terminated':True}),'utf-8')
                raise TimeoutError('Isolated task still running; retained for safe inspection, not killed')
            time.sleep(.2)
    report=json.loads((out/'exe-report.json').read_text('utf-8'));report['process_exit']=process.returncode
    if a.exe:
        with a.exe.open('rb') as stream:report['exe_sha256']=hashlib.file_digest(stream,'sha256').hexdigest()
    report['accepted']=report['passed'] and process.returncode==0
    (out/'process-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps(report,ensure_ascii=False))
    if not report['accepted']:raise SystemExit(1)

if __name__=='__main__':main()
