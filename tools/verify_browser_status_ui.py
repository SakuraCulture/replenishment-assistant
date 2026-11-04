"""Run actual source/EXE connection-indicator UI against isolated simulated transports."""
import argparse,hashlib,json,os,subprocess,sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def main():
    p=argparse.ArgumentParser();p.add_argument('--exe',type=Path);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    env={**os.environ,'REPLENISHMENT_DATA_DIR':str(out/'user'),'PYTHONUTF8':'1'}
    command=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    with (out/'process.log').open('w',encoding='utf-8') as log:
        result=subprocess.run(command+['--browser-status-smoke',str(out)],env=env,stdout=log,stderr=log,timeout=90)
    report=json.loads((out/'report.json').read_text('utf-8'))
    report.update(process_exit=result.returncode,exe_sha256=hashlib.sha256(a.exe.read_bytes()).hexdigest() if a.exe else None)
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n','utf-8')
    print(json.dumps(report,ensure_ascii=False))
    if result.returncode or not report['passed'] or (a.exe and not report.get('frozen')):raise SystemExit(1)

if __name__=='__main__':main()
