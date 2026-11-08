"""Create a fresh isolated fixture and exercise the source or packaged number editor."""
import argparse, copy, hashlib, json, os, sqlite3, subprocess, sys, time
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tools')]


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--out',type=Path,required=True); p.add_argument('--exe',type=Path); a=p.parse_args()
    out=a.out.resolve(); out.relative_to(ROOT/'qa')
    from verify_payment_matching import fixture
    import jst_orders as jst, order_lifecycle as lc
    path=fixture(out); f=json.loads((out/'matching-fixture.json').read_text('utf-8'))
    head=jst.get(path,f['gids'][0])['head']; unpaid=jst.get(path,f['gids'][-1])['head']
    lc.commit(path,jst.payment_plan(path,f['gids'][0]))
    with closing(sqlite3.connect(path)) as db:
        rows=copy.deepcopy(lc.current(db,head,'payment')['payload']['raw_rows'])
        plan=lc.make_plan(db,lc.full_head(path,head),'receipt',rows,[])
    lc.commit(path,{'plans':[plan],'files':[]})
    (out/'reference-fixture.json').write_text(json.dumps({'head':head,'unpaid':unpaid,'synthetic':True},ensure_ascii=False),'utf-8')
    env={**os.environ,'REPLENISHMENT_DATA_DIR':str(out/'settings'),'LOCALAPPDATA':str(out/'localappdata'),'PYTHONUTF8':'1','QT_QPA_PLATFORM':'windows'}
    cmd=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    with (out/'process.log').open('wb') as log:
        proc=subprocess.Popen(cmd+['--payment-reference-smoke',str(out)],cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW); started=time.monotonic()
        while proc.poll() is None:
            if time.monotonic()-started>180:raise TimeoutError(f'Isolated process {proc.pid} retained; no forced termination')
            time.sleep(.2)
    report=json.loads((out/'report.json').read_text('utf-8')); report['process_exit']=proc.returncode
    if a.exe:
        with a.exe.open('rb') as f:report['exe_sha256']=hashlib.file_digest(f,'sha256').hexdigest()
    (out/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps(report,ensure_ascii=False)); return 0 if report['passed'] and proc.returncode==0 else 1

if __name__=='__main__':sys.exit(main())
