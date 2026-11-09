"""New isolated output only; run the same closed Qt probe in source or EXE."""
import argparse,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from test_aoxiang_acquisition import configuration
from aoxiang_jobs import Jobs
from aoxiang_acquisition import prepare

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--exe',type=Path);a=p.parse_args()
    out=a.out.resolve();out.relative_to(ROOT/'qa');out.mkdir(parents=True,exist_ok=False)
    jobs=Jobs(out/'settings/aoxiang/jobs.sqlite3');conf=configuration();day='2026-11-09'
    job=prepare(jobs,conf,['W2','W1'],day=day,freshness='refresh')
    record={'id':job['id'],'request':job['command'],'fingerprint':job['fingerprint'],'sequence':2,'state':'NEEDS_REVIEW',
        'items':[{'index':i,'warehouse_code':code,'run_id':'synthetic-'+code,'state':'NEEDS_REVIEW' if i==0 else 'PENDING',
                  'error':{'code':'UI_ACTION_TIMEOUT','message':'合成旧版菜单超时'} if i==0 else None,'result':None} for i,code in enumerate(['W2','W1'])]}
    jobs.observe(job['id'],record)
    (out/'fixture.json').write_text(json.dumps({'synthetic':True,'configuration':conf,'record':record,'day':day},ensure_ascii=False),'utf-8')
    cmd=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    env={**os.environ,'REPLENISHMENT_DATA_DIR':str(out/'settings'),'PYTHONUTF8':'1'}
    with (out/'process.log').open('w',encoding='utf-8') as f:
        r=subprocess.run(cmd+['--export-menu-smoke',str(out)],stdout=f,stderr=subprocess.STDOUT,env=env,timeout=90,creationflags=subprocess.CREATE_NO_WINDOW)
    report=json.loads((out/'acceptance.json').read_text('utf-8'));assert r.returncode==0 and report['passed'],report
    print(json.dumps(report,ensure_ascii=False))
if __name__=='__main__':main()
