"""Serial alternating measurements; synthetic inputs; no browser/platform operations.

Manual reference uses the fixed pre-change source and existing load/run/save services.
The automatic path includes independent task persistence. Fixture construction is untimed.
"""
import argparse,copy,gc,json,os,platform,shutil,statistics,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def save(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2),encoding='utf-8')
def read(p):return json.loads(p.read_text('utf-8'))
def stats(values):
    ordered=sorted(values)
    return dict(values=values,min=min(values),median=statistics.median(values),max=max(values),p95=ordered[max(0,__import__('math').ceil(len(values)*.95)-1)])

def fixture(folder,count,skus=200):
    sys.path.insert(0,str(ROOT/'src'))
    import work_repository as wr,warehouse_catalog as wc,store_parameter_import as spi,replenishment_plans as plans
    import scheduled_execution as se
    from openpyxl import Workbook
    folder.mkdir(parents=True);work=wr.Repository(folder)
    master=folder/'synthetic-master.xlsx';b=Workbook();w=b.active;w.append(wc.REQUIRED+['商品标签','商品名称'])
    for i in range(skus):w.append([f'{i:08}','饮品',2+i%5,6 if i%2 else 1,'启用',1000,'',f'合成{i}'])
    b.save(master);b.close();catalog=wc.import_catalog(master,'山东仓');wc.Repository(folder).save(catalog)
    files=[];stores=[]
    from input_adapter import digest
    for i in range(count):
        code=f'S{i:03}';name='合成门店'+str(i);stores.append(dict(code=code,name=name))
        record=spi.base();record['ui'].update(mode=1,primary='山东仓',secondary=None);work.save_stores({code:record})
        p=folder/(code+'.xlsx');b=Workbook();w=b.active
        w.append(['仓库/门店编码','仓库/门店名称','商品编码','商品名称','日销','周销','月销','可用库存','在途库存','缺货天数（天）','补货状态'])
        for j in range(skus):w.append([code,name,f'{j:08}',f'合成{j}',2,14,60,j%10,0,0,'正常'])
        b.save(p);b.close();files.append(dict(file=str(p),warehouse_code=code,sha256=digest(p)))
    plan=plans.Repository(work.path).save(dict(name='合成性能',stores=stores,config=spi.base()['preset']['config'],parameter_fields=[]))
    schedule=dict(name='合成性能',plan_id=plan['id'],stores=stores,time='06:30',kind='weekly',weekdays=list(range(7)),workers=2,enabled=False)
    import work_repository
    basis=se.freeze(folder,schedule,work_repository.today());schedule['check_signature']=se.check_signature(basis)
    save(folder/'benchmark-input.json',dict(schedule=schedule,basis=basis,catalogs={'山东仓':catalog},manifest={'files':files}))

def worker(a):
    source=(ROOT/'qa/v0370/baseline/src') if a.mode=='manual' else ROOT/'src'
    sys.path.insert(0,str(source));sys.path.insert(0,str(ROOT/'qa/export_071/qa-deps'))
    import batch_service,work_repository as wr,psutil
    from source_archive import retained_import
    from version_info import MODEL_VERSION
    data=read(a.folder/'benchmark-input.json');basis=data['basis'];catalogs=data['catalogs'];manifest=data['manifest']
    work=wr.Repository(a.folder);stages={};start=time.perf_counter();stable=[];outputs=[]
    def track(name,fn):
        def wrapped(*args,**kw):
            t=time.perf_counter()
            try:return fn(*args,**kw)
            finally:stages[name]=stages.get(name,0)+time.perf_counter()-t
        return wrapped
    batch_service.load=track('read_prepare',batch_service.load);batch_service.run=track('compute',batch_service.run)
    if a.mode=='manual':
        import warehouse_catalog
        for c in catalogs.values():warehouse_catalog.Repository(a.folder).save(c)
        loaded=batch_service.load([f['file'] for f in manifest['files']],catalogs)
        retained_import(batch_service.load,(),loaded,work.path,lambda _:None,None)
        results=batch_service.run(loaded,basis['jobs'],basis['profile'],2)
        records=basis['records'];payload=dict(schema=1,day=basis['day'],model_version=MODEL_VERSION,input_dirty=False,
            active_plan=basis['plan'],admission_required=True,loaded=loaded,catalogs=catalogs,profile=basis['profile'],new_focus=basis['new_focus'],results=results,
            states={s:dict(name=loaded['stores'][s],state='已完成',error='') for s in results},store_presets={s:r['preset'] for s,r in records.items()},store_ui={s:r['ui'] for s,r in records.items()},overrides={})
        t=time.perf_counter();work.save_job(basis['day'],payload);stages['persist']=time.perf_counter()-t
        outputs=[dict(stores=len(results),amount=sum(r['summary']['amount'] for r in results.values()))]
    else:
        import scheduled_replenishment as sr,scheduled_execution as se
        repo=sr.Repository(work.path);repo.save_value=track('persist',repo.save_value)
        class Backend:
            def acquire(self):pass
            def release(self,p):pass
            def sales(self,r,b):return manifest
            def catalogs(self,r,b):
                for w,c in catalogs.items():repo.save_value(r.rid,'catalog:'+w,c,r.epoch)
                return catalogs
        for i in range(a.repeat):
            r=repo.manual(data['schedule']);se.Runner(repo,Backend(),a.folder).execute(r['id']);r=repo.get(r['id'])
            if r['state']!='COMPLETE':raise ValueError(r['message'])
            se.publish_catalogs(repo,r['id'],a.folder)
            outputs.append(dict(stores=len(r['stores']),amount=sum(s['amount'] for s in r['stores'])))
            repo.request_pause(r['id'],True);gc.collect();stable.append(psutil.Process().memory_info().rss/1048576)
    save(a.folder/'measurement.json',dict(seconds=time.perf_counter()-start,stages=stages,outputs=outputs,steady_rss_mib=stable,source=str(source)))

def measure(folder,mode,repeat=1):
    sys.path.insert(0,str(ROOT/'qa/export_071/qa-deps'));import psutil
    cmd=[sys.executable,str(Path(__file__).resolve()),'--worker','--folder',str(folder),'--mode',mode,'--repeat',str(repeat)]
    start=time.perf_counter();samples=[]
    with (folder/'process.log').open('w',encoding='utf-8') as log:
        proc=subprocess.Popen(cmd,stdout=log,stderr=log,env={**os.environ,'REPLENISHMENT_DATA_DIR':str(folder),'PYTHONUTF8':'1'})
        while proc.poll() is None:
            try:processes=[psutil.Process(proc.pid)]+psutil.Process(proc.pid).children(recursive=True)
            except psutil.Error:processes=[]
            total=0
            for p in processes:
                try:total+=p.memory_info().rss
                except psutil.Error:pass
            samples.append(total/1048576);time.sleep(.25)
        if proc.returncode:raise RuntimeError(str(folder/'process.log'))
    result=read(folder/'measurement.json');result.update(wall_seconds=time.perf_counter()-start,peak_rss_mib=max(samples),rss_samples_mib=samples)
    save(folder/'measurement.json',result);return result

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path);p.add_argument('--worker',action='store_true');p.add_argument('--folder',type=Path);p.add_argument('--mode',choices=['manual','auto']);p.add_argument('--repeat',type=int,default=1);a=p.parse_args()
    if a.worker:return worker(a)
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    report=dict(scope='All workload records synthetic; 200 SKU per store. Native network waiting excluded. Windows OS file cache not flushed. Serial alternating three trials, two computation processes. End-to-end includes startup, read, prepare, compute and result storage.',environment=dict(python=sys.version,platform=platform.platform(),cpu_count=os.cpu_count()),cases={},passed=True)
    for count in (1,10,30):
        source=out/f'fixture-{count}';fixture(source,count);raw={'manual':[],'auto':[]}
        for trial in range(3):
            for mode in (('manual','auto') if trial%2==0 else ('auto','manual')):
                folder=out/f'{count}-{trial}-{mode}';shutil.copytree(source,folder);r=measure(folder,mode);raw[mode].append(r)
        summary={mode:dict(seconds=stats([r['seconds'] for r in values]),wall=stats([r['wall_seconds'] for r in values]),peak_rss_mib=stats([r['peak_rss_mib'] for r in values])) for mode,values in raw.items()}
        equality=all(r['outputs']==raw['manual'][0]['outputs'] for v in raw.values() for r in v)
        extra=summary['auto']['seconds']['median']-summary['manual']['seconds']['median'];limit=max(2,.1*summary['manual']['seconds']['median'])
        report['cases'][str(count)]=dict(rows=count*200,raw=raw,summary=summary,summary_equal=equality,overhead_seconds=extra,overhead_target_met=extra<=limit)
        report['passed'] &= equality and extra<=limit;save(out/'report.json',report);print(count,summary,flush=True)
    source=out/'fixture-1';folder=out/'repeat-20';shutil.copytree(source,folder);repeat=measure(folder,'auto',20);steady=repeat['steady_rss_mib'];growth=statistics.median(steady[-5:])-statistics.median(steady[:5])
    report['repeat20']=dict(measurement=repeat,growth_mib=growth,target_met=growth<=max(50,.1*statistics.median(steady[:5])));report['passed'] &= report['repeat20']['target_met']
    import scheduled_replenishment as sr,work_repository as wr
    repo=sr.Repository(wr.Repository(out/'summaries').path);repo.ensure();schedule=read(source/'benchmark-input.json')['schedule']
    with repo.db(True) as db:
        for i in range(10000):repo._create(db,schedule,'manual:perf-'+str(i),repo.clock(),missed=True)
    times=[]
    for _ in range(20):
        t=time.perf_counter();page=repo.list(limit=50);times.append(time.perf_counter()-t)
        if len(page['items'])!=50 or page['total']!=10000:raise AssertionError('pagination')
    report['summary_query_10000']=stats(times);report['passed'] &= report['summary_query_10000']['p95']<=1
    save(out/'report.json',report);print(json.dumps(dict(passed=report['passed'],report=str(out/'report.json'))))

if __name__=='__main__':
    import multiprocessing
    multiprocessing.freeze_support();main()
