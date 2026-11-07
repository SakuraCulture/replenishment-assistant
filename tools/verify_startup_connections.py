"""Isolated actual-window startup acceptance. Never reads business settings or logs in."""
import argparse,hashlib,json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--exe',type=Path);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    env={**os.environ,'REPLENISHMENT_DATA_DIR':str(out/'settings'),'PYTHONIOENCODING':'utf-8'};env.pop('QT_QPA_PLATFORM',None)
    cmd=[str(args.exe.resolve())] if args.exe else [sys.executable,str(ROOT/'src/app.py')]
    started=time.monotonic()
    with (out/'process.log').open('w',encoding='utf-8') as log:
        process=subprocess.Popen([*cmd,'--startup-connections-smoke',str(out)],cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:code=process.wait(180)
        except subprocess.TimeoutExpired:
            (out/'pending.json').write_text(json.dumps({'pid':process.pid,'state':'Still running; not terminated'}),'utf-8')
            raise RuntimeError('Acceptance still running; inspect before another attempt')
    report=json.loads((out/'report.json').read_text('utf-8'));report.update(process_exit=code,seconds=time.monotonic()-started)
    if args.exe:
        with args.exe.open('rb') as stream:report['exe_sha256']=hashlib.file_digest(stream,'sha256').hexdigest()
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n','utf-8',newline='\n')
    print(json.dumps({k:report.get(k) for k in ['passed','process_exit','seconds','error']},ensure_ascii=False))
    if code or not report['passed'] or (args.exe and not report.get('frozen_exe')):raise SystemExit(1)


if __name__=='__main__':main()
