"""Source and immutable EXE acceptance in isolated synthetic settings."""
import argparse,hashlib,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    p=argparse.ArgumentParser();p.add_argument('--exe',type=Path);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);(out/'synthetic.json').write_text('{"synthetic":true}','utf-8')
    env={**os.environ,'REPLENISHMENT_DATA_DIR':str(out/'user'),'PYTHONUTF8':'1'}
    command=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    with (out/'process.log').open('w',encoding='utf-8') as log:r=subprocess.run(command+['--scheduled-smoke',str(out)],env=env,stdout=log,stderr=log,timeout=180)
    report=json.loads((out/'report.json').read_text('utf-8'));report.update(process_exit=r.returncode,exe_sha256=hashlib.sha256(a.exe.read_bytes()).hexdigest() if a.exe else None)
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');print(json.dumps(report,ensure_ascii=False))
    if r.returncode or not report['passed']:raise SystemExit(1)
if __name__=='__main__':main()
