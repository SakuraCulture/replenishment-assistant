"""New isolated output only; run source or EXE pricing acceptance without platforms."""
import argparse,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True);p.add_argument('--exe');a=p.parse_args()
    out=Path(a.output).resolve();out.mkdir(parents=True,exist_ok=False)
    env={**os.environ,'REPLENISHMENT_DATA_DIR':str(out/'settings'),'PYTHONUTF8':'1','QT_QPA_PLATFORM':'windows'}
    command=[str(Path(a.exe).resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    with (out/'process.log').open('wb') as f:r=subprocess.run([*command,'--purchase-pricing-smoke',str(out)],cwd=ROOT,env=env,stdout=f,stderr=f)
    report=json.loads((out/'report.json').read_text('utf-8'))
    print(json.dumps({'passed':report['passed'],'checks':len(report['checks']),'exit_code':r.returncode,'output':str(out)},ensure_ascii=False))
    return 0 if report['passed'] and r.returncode==0 else 1
if __name__=='__main__':sys.exit(main())
