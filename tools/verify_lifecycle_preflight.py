"""Exercise real dialogs and local queue in isolated source/packaged applications."""
import argparse, copy, hashlib, json, os, shutil, subprocess, sys, time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'tests')]


def fixture(out):
    import test_archive_orders, normal_orders as no, order_lifecycle as lc, work_repository as wr
    work, _, template = test_archive_orders.seed(out)
    with closing(work.connect()) as db, db:
        db.execute('DELETE FROM normal_orders'); db.execute('DELETE FROM normal_revisions')
        lc.schema(db)
        for i in range(13):
            oid, store = 'preflight-'+str(i), 'S%02d' % i
            payload = copy.deepcopy(template); result = payload['result']
            result.update(store=store, store_name='核对测试门店'+str(i)); result['rows']=result['rows'][:1]
            db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',
                       (wr.today(), store, 'normal_model', oid, 'BH-'+store, 1))
            db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',
                       (oid, 1, no.fingerprint(result), wr.now(), no.encode(payload, db)))
            if i < 12:
                for stage in ('return','operations'):
                    doc={'schema':1,'files':[],'rows':[{'sku':'0001','name':'核对商品','base_quantity':'5'}]}
                    db.execute('INSERT INTO lifecycle_versions VALUES(?,?,?,?,?,?,?,?)',
                               (oid,1,'河北仓',stage,1,wr.now(),lc.fingerprint(doc),wr.dumps(doc)))
    heads=[h for h in lc.current_targets(work.path) if h['warehouse']=='河北仓']
    heads.sort(key=lambda h:h['store'])
    # Three distinct local preparation causes, all with real retained documents.
    with closing(work.connect()) as db, db:
        for index, pending, changed in ((1,True,False),(2,False,True)):
            head=heads[index]; basis=lc.upstream(db,head,'payment')
            if changed: basis['dependencies']['return']['version']=0
            doc={'schema':lc.SCHEMA,'stage':'payment','pending':pending,'basis':basis,'files':[],
                 'rows':[{'sku':'0001','name':'核对商品','base_quantity':'5','amount':'10','currency':'CNY'}]}
            db.execute('INSERT INTO lifecycle_versions VALUES(?,?,?,?,?,?,?,?)',
                       (head['oid'],1,'河北仓','payment',1,wr.now(),lc.fingerprint(doc),wr.dumps(doc)))
    (out/'fixture.json').write_text(json.dumps({'heads':heads,'day':wr.today()},ensure_ascii=False,indent=2),'utf-8',newline='\n')
    (out/'synthetic.json').write_text(json.dumps({'synthetic':True,'live_platform':False,'purpose':'lifecycle-payment-preflight'}),'utf-8',newline='\n')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',type=Path,required=True); p.add_argument('--exe',type=Path); p.add_argument('--timeout',type=float,default=130)
    a=p.parse_args(); out=a.out.resolve(); out.mkdir(parents=True,exist_ok=False); fixture(out)
    env=os.environ.copy()
    for n in ('PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','REPLENISHMENT_DATA_DIR','REPLENISHMENT_PORTABLE_PATHS','QT_QPA_PLATFORM'): env.pop(n,None)
    env.update(PYTHONUTF8='1',REPLENISHMENT_DATA_DIR=str(out/'settings'),LOCALAPPDATA=str(out/'localappdata'),APPDATA=str(out/'roaming'))
    if not a.exe: env['QT_QPA_PLATFORM']='offscreen'
    cmd=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    report={'passed':False,'synthetic_input':True,'live_platform':False,'runs':[],
            'exe_sha256':hashlib.sha256(a.exe.read_bytes()).hexdigest() if a.exe else None}
    started=time.monotonic()
    for mode in ('exercise','reopen'):
        with (out/(mode+'-process.log')).open('wb') as log:
            proc=subprocess.Popen(cmd+['--lifecycle-preflight-smoke',str(out),mode],cwd=out,env=env,stdout=log,stderr=log)
            try: code=proc.wait(a.timeout)
            except subprocess.TimeoutExpired:
                report.update(error='Synthetic application timed out; not forcibly terminated',pid=proc.pid)
                (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8',newline='\n'); raise
        f=out/(mode+'-report.json'); r=json.loads(f.read_text('utf-8')) if f.exists() else {'passed':False,'error':'Native report missing'}
        r['exit_code']=code; report['runs'].append(r)
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8',newline='\n')
        if code or not r['passed']: raise AssertionError(r)
    report.update(passed=True,elapsed_seconds=time.monotonic()-started)
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8',newline='\n')
    print(json.dumps({'passed':True,'checks':sum(len(r['checks']) for r in report['runs']),'report':str(out/'report.json')},ensure_ascii=False))


if __name__=='__main__': main()
