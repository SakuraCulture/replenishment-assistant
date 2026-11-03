"""Launch isolated actual application policy verification (source or frozen EXE)."""
import argparse,hashlib,json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    p=argparse.ArgumentParser();p.add_argument('--exe',type=Path);p.add_argument('--fixture',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    env=os.environ.copy();env['REPLENISHMENT_DATA_DIR']=str(out/'settings');env['PYTHONUTF8']='1';env.pop('QT_QPA_PLATFORM',None);env['QT_SCALE_FACTOR']='1'
    command=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')];started=time.perf_counter()
    with (out/'process.txt').open('w',encoding='utf-8') as f:
        proc=subprocess.run(command+['--restock-smoke',str(a.fixture.resolve()),str(out)],env=env,stdout=f,stderr=f,timeout=240)
    r=json.loads((out/'switch-report.json').read_text('utf-8'));r.update(exit_code=proc.returncode,seconds=time.perf_counter()-started,exe_sha256=hashlib.sha256(a.exe.read_bytes()).hexdigest() if a.exe else None)
    (out/'switch-report.json').write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(r,ensure_ascii=False))
    if proc.returncode or not r.get('passed') or a.exe and not r.get('frozen_exe'):raise SystemExit(1)
if __name__=='__main__':main()
