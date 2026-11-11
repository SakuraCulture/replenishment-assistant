"""New isolated fixture each run. No real platform, no existing data directory."""
import argparse,copy,json,os,sqlite3,subprocess,sys,time
from pathlib import Path
from contextlib import closing
ROOT=Path(__file__).resolve().parents[1]

def main():
    p=argparse.ArgumentParser();p.add_argument('--exe',type=Path);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
    from test_archive_orders import seed
    import normal_orders as no,work_repository as wr
    w,repo,payload=seed(out);payload=copy.deepcopy(payload);r=payload['result'];r.update(store='U',store_name='未选保留店')
    for d in r['rows']:d['B']='U'
    with closing(w.connect()) as db,db:
        db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(wr.today(),'U','normal_model','other','BH-U-ZC',1))
        db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',('other',1,no.fingerprint(r),wr.now(),no.encode(payload,db)))
    (out/'fixture.json').write_text(json.dumps({'synthetic':True,'no_live_platform':True}),'utf-8')
    command=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    env={**os.environ,'REPLENISHMENT_DATA_DIR':str(out/'settings'),'PYTHONUTF8':'1'};env.pop('QT_QPA_PLATFORM',None)
    started=time.monotonic()
    with (out/'process.log').open('w',encoding='utf-8') as log:
        process=subprocess.Popen([*command,'--deletion-lifecycle-smoke',str(out)],cwd=ROOT,env=env,stdout=log,stderr=log)
        try:code=process.wait(timeout=180)
        except subprocess.TimeoutExpired:
            (out/'pending-process.json').write_text(json.dumps({'pid':process.pid,'not_terminated':True}),'utf-8');raise
    report=json.loads((out/'acceptance.json').read_text('utf-8'));report.update(process_exit=code,seconds=time.monotonic()-started)
    (out/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps(report,ensure_ascii=False))
    if code or not report['passed'] or a.exe and not report['frozen_exe']:raise AssertionError('Acceptance failed')

if __name__=='__main__':main()
