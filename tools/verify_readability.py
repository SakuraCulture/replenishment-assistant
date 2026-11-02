"""Re-run isolated actual source/EXE window checks. No live settings or platform."""
import argparse,hashlib,json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--exe',type=Path);p.add_argument('--fixture',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);env=os.environ.copy();env['REPLENISHMENT_DATA_DIR']=str(out/'settings');env['PYTHONUTF8']='1';env.pop('QT_QPA_PLATFORM',None);env['QT_SCALE_FACTOR']='1'
    cmd=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    start=time.perf_counter()
    with (out/'process.txt').open('w',encoding='utf-8') as f:r=subprocess.run(cmd+['--readability-smoke',str(a.fixture.resolve()),str(out)],env=env,stdout=f,stderr=f,timeout=180)
    report=json.loads((out/'readability-report.json').read_text('utf-8'));report.update(exit_code=r.returncode,total_seconds=time.perf_counter()-start,exe_sha256=hashlib.sha256(a.exe.read_bytes()).hexdigest() if a.exe else None)
    (out/'readability-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
    if r.returncode or not report['passed'] or a.exe and not report['frozen_exe']:raise SystemExit(1)


if __name__=='__main__':main()
