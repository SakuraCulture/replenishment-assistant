"""Two-phase native/executable repair acceptance using only new synthetic roots."""
import argparse,copy,hashlib,json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/p) for p in ('src','tests')]

def catalog(root,bid,profile):
    from catalog_sync_client import Client
    from stockout_common import atomic_json
    folder=root/'jst_catalog_sync';c=Client(folder);c.start();assert c.close()
    data=json.loads((folder/'jobs.json').read_text('utf-8'))
    account={'id':'sd','warehouse':'山东仓','expectedCompany':'QA company','expectedCoid':'11','expectedUid':'22','mappingVerified':True,'bindingVersion':1,'enabled':True,'binding':{'actualProfilePath':str(profile/'Default')}}
    data.update(accounts={'山东仓':account},batches={bid:{'id':bid,'createdAt':'2026-11-12','purpose':'default_catalog','state':'STOPPED','items':[
        {'id':'one','account':account,'stage':'ACCEPTED','native':{'stage':'ACCEPTED','submissions':1,'nativeTaskRef':'ORIGINAL-'+bid,'scope':{'expectedRows':1,'coid':'11','uid':'22'}},'receipt':None,'adoption':None}]}})
    atomic_json(folder/'jobs.json',data)

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--exe',type=Path);a=p.parse_args()
    out=a.out.resolve();out.relative_to(ROOT/'qa');out.mkdir(exist_ok=False,parents=True)
    import test_order_analytics as seed,work_repository as wr,store_parameter_import as spi,parameter_tables,order_migration
    from contextlib import closing
    import sqlite3
    settings=out/'settings';settings.mkdir();f=seed.AnalyticsTests();f.setUp()
    try:
        for row in f.p['result']['rows']:row['V']=None
        f.save_payload()
        with closing(sqlite3.connect(f.path)) as source,closing(sqlite3.connect(settings/'workbench.sqlite3')) as target:source.backup(target)
    finally:f.tearDown()
    record=spi.base();record['name']='测试A';record['ui'].update(primary='山东仓',secondary='河北仓',mode=1)
    wr.Repository(settings).save_stores({'A':record});changed=copy.deepcopy(record);changed['preset']['config']['parameters']['minimum_reference_units']=3
    parameter_tables.write(out/'changed-parameters.xlsx','complete_parameters',{'A':changed},scoped=True)
    source=out/'source-settings';source.mkdir();source_repo=wr.Repository(source);restored=copy.deepcopy(record);restored['preset']['config']['parameters']['minimum_reference_units']=7;source_repo.save_stores({'A':restored})
    catalog(source,'SOURCE-TASK',out/'source-private-profile');catalog(settings,'TARGET-OLD',out/'target-local-profile')
    order_migration.export(source,out/'source-orders.zip')
    (out/'synthetic.json').write_text(json.dumps({'synthetic':True,'live_platform':False}),'utf-8')
    command=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    env={**os.environ,'PYTHONUTF8':'1','QT_QPA_PLATFORM':'windows','LOCALAPPDATA':str(out/'localappdata'),'REPLENISHMENT_DATA_DIR':str(settings)}
    for phase in ('exercise','reopen'):
        with (out/(phase+'-process.log')).open('wb') as log:
            proc=subprocess.Popen(command+['--business-repairs-smoke',str(out),phase],cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW);started=time.monotonic()
            while proc.poll() is None:
                if time.monotonic()-started>180:raise TimeoutError(f'Isolated QA PID {proc.pid} retained; no forced termination')
                time.sleep(.1)
        report=json.loads((out/(phase+'-report.json')).read_text('utf-8'));assert proc.returncode==0 and report['passed'],report
    reports=[json.loads((out/(phase+'-report.json')).read_text('utf-8')) for phase in ('exercise','reopen')]
    result={'passed':True,'frozen_exe':bool(a.exe),'exe_sha256':hashlib.sha256(a.exe.read_bytes()).hexdigest() if a.exe else None,
            'checks':[check for r in reports for check in r['checks']],'version':reports[0]['version'],'synthetic':True,'live_platform':False,'phases':reports}
    (out/'acceptance.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n','utf-8',newline='\n');print(json.dumps({k:v for k,v in result.items() if k!='phases'},ensure_ascii=False))
if __name__=='__main__':main()
