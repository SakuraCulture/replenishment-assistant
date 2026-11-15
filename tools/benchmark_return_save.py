"""Alternating isolated old/new lifecycle preparation/save equivalence benchmark."""
import argparse,gc,gzip,hashlib,json,os,shutil,sqlite3,statistics,subprocess,sys,threading,time,traceback
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n','utf-8',newline='\n')
def sha(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def clone(source,target):
    with closing(sqlite3.connect(source)) as src,closing(sqlite3.connect(target)) as dst:src.backup(dst)


def worker(a):
    sys.path[:0]=[str(a.source/'src'),str(a.source/'tests'),str(a.deps)]
    import psutil,work_repository as wr,order_lifecycle as lc,lifecycle_io as io,purchases,daily_batch
    from unittest.mock import patch
    assert json.loads((a.fixture/'synthetic.json').read_text('utf-8'))['synthetic']
    a.out.mkdir(parents=True,exist_ok=False)
    scans={}
    for stage in ('return','operations'):
        file=a.fixture/'fixture'/(stage+'.xlsx');info=io.inspect([file])[0];sheet=info['sheets'][0]
        scans[stage]=io.scan(stage,[{**info,**sheet,'mapping':{h:i for i,h in enumerate(sheet['headers']) if h in io.fields(stage)}}])
    def prepare(path,stage,heads):
        by_number={h['number']:h for h in heads}
        return lc.prepare(path,scans[stage],{lc.binding_key(r):by_number[r['signed_number']] for r in scans[stage]['rows']})
    if a.setup:
        db=a.out/'complete.sqlite3';clone(a.fixture/'operations-base.sqlite3',db)
        heads=purchases.targets(db)
        with patch('work_repository.now',lambda:'2026-11-15T12:00:00+08:00'):lc.commit(db,prepare(db,'operations',heads))
        write(a.out/'report.json',{'passed':True,'synthetic':True,'path':str(db),'sha256':sha(db)});return
    cases=[];process=psutil.Process()
    def facts(path,dump=None):
        signatures={}; stream=gzip.open(dump,'wt',encoding='utf-8') if dump else None
        try:
            with closing(sqlite3.connect(path)) as db:
                assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
                for table,sql in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
                    digest=hashlib.sha256();count=0
                    for row in db.execute('SELECT * FROM "'+table+'" ORDER BY rowid'):
                        value=[{'blob':v.hex()} if isinstance(v,bytes) else v for v in row];text=wr.dumps(value)
                        digest.update(text.encode('utf-8'));digest.update(b'\n');count+=1
                        if stream:stream.write(wr.dumps([table,value])+'\n')
                    signatures[table]={'rows':count,'sha256':digest.hexdigest()}
        finally:
            if stream:stream.close()
        return signatures
    sources={'return_initial':a.fixture/'fixture/settings/workbench.sqlite3','operations_initial':a.fixture/'operations-base.sqlite3',
             'return_unchanged':a.fixture/'operations-base.sqlite3','operations_unchanged':a.complete}
    for name,source in sources.items():
        stage='operations' if name.startswith('operations') else 'return';folder=a.out/name;folder.mkdir();working=folder/'isolated-db';working.mkdir();db=working/'workbench.sqlite3';clone(source,db)
        heads=purchases.targets(db);request={'from':wr.today(),'to':wr.today(),'stage_view':stage,'stage_filter':'本环节全部','explain_filters':True}
        daily_batch.read(db,working/'projection.sqlite3',request)  # Existing page's warm signing projection.
        gc.collect();peak=[process.memory_info().rss];stop=threading.Event()
        def sample():
            while not stop.wait(.02):peak[0]=max(peak[0],process.memory_info().rss)
        sampler=threading.Thread(target=sample);sampler.start();begin=lc.begin;times={};events=[]
        def checked_begin(*args,**kwargs):
            t=time.perf_counter();value=begin(*args,**kwargs);times['write_lock_wait_seconds']=time.perf_counter()-t;times['_acquired']=time.perf_counter();return value
        try:
            t=time.perf_counter()
            if name.endswith('unchanged'):
                plans=[lc.recheck(db,h,stage) for h in heads]
                plan={'plans':[p for x in plans for p in x['plans']],'files':list({f['sha']:f for x in plans for f in x['files']}.values()),'archived_sources':True}
                prepare_kind='archived_recheck'
            else:plan=prepare(db,stage,heads);prepare_kind='file_prepare'
            times['prepare_seconds']=time.perf_counter()-t
            clean={**plan,'plans':[{k:v for k,v in p.items() if k!='_read_proofs'} for p in plan['plans']]}
            plan_sha=hashlib.sha256(wr.dumps(clean).encode('utf-8')).hexdigest();del clean
            before=process.io_counters();t=time.perf_counter()
            with patch('work_repository.now',lambda:'2026-11-15T12:00:00+08:00'),patch('order_lifecycle.begin',checked_begin):
                receipts=lc.commit(db,plan,progress=lambda x:events.append({'seconds':time.perf_counter()-t,'message':str(x)}))
            end=time.perf_counter();after=process.io_counters();times['save_seconds']=end-t;times['write_lock_through_close_seconds']=end-times.pop('_acquired')
        finally:stop.set();sampler.join()
        t=time.perf_counter();view=daily_batch.read(db,working/'projection.sqlite3',request);times['post_save_warm_list_seconds']=time.perf_counter()-t
        result={'case':name,'prepare_kind':prepare_kind,'times':times,'peak_rss_mib':peak[0]/2**20,'process_read_mib':(after.read_bytes-before.read_bytes)/2**20,
                'process_write_mib':(after.write_bytes-before.write_bytes)/2**20,'plan_sha256':plan_sha,'receipts':receipts,'view_sha256':hashlib.sha256(wr.dumps(view).encode()).hexdigest(),
                'tables':facts(db,folder/'business-facts.json.gz' if a.trial==1 else None),'events':events}
        write(folder/'report.json',result);cases.append(result)
        # Delete only this run's explicitly marked, reproducible synthetic DB copy.
        assert working.resolve().is_relative_to(a.out.resolve()) and working.name=='isolated-db'
        shutil.rmtree(working)
        print(json.dumps({'variant':a.variant,'trial':a.trial,'case':name,'times':times,'rss_mib':peak[0]/2**20}),flush=True)
    write(a.out/'report.json',{'passed':True,'cases':cases,'variant':a.variant,'trial':a.trial,'source':str(a.source)})


def main():
    p=argparse.ArgumentParser();p.add_argument('--baseline',type=Path);p.add_argument('--source',type=Path);p.add_argument('--fixture',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--deps',type=Path,required=True);p.add_argument('--complete',type=Path);p.add_argument('--worker',action='store_true');p.add_argument('--setup',action='store_true');p.add_argument('--variant');p.add_argument('--trial',type=int,default=1);p.add_argument('--repeats',type=int,default=3);a=p.parse_args()
    if a.worker:return worker(a)
    a.out=a.out.resolve();a.out.mkdir(parents=True,exist_ok=False)
    source=ROOT;baseline=a.baseline.resolve();measurements=[]
    def run(cmd,log):
        with log.open('w',encoding='utf-8') as stream:
            result=subprocess.run([sys.executable,__file__,*cmd],stdout=stream,stderr=subprocess.STDOUT,env={**os.environ,'PYTHONUTF8':'1'})
        if result.returncode:raise RuntimeError('Benchmark worker failed; original log retained: '+str(log))
    shared=['--worker','--fixture',str(a.fixture.resolve()),'--deps',str(a.deps.resolve())]
    run([*shared,'--setup','--source',str(baseline),'--out',str(a.out/'setup')],a.out/'setup.log')
    complete=a.out/'setup/complete.sqlite3'
    for trial in range(1,a.repeats+1):
        for variant in (('old','new') if trial%2 else ('new','old')):
            out=a.out/f'{trial}-{variant}'
            run([*shared,'--source',str(baseline if variant=='old' else source),'--complete',str(complete),'--out',str(out),'--variant',variant,'--trial',str(trial)],a.out/f'{trial}-{variant}.log')
            measurements.append(json.loads((out/'report.json').read_text('utf-8')));print(f'completed trial {trial} {variant}',flush=True)
    signatures={}
    for run_data in measurements:
        for case in run_data['cases']:
            expected={k:case[k] for k in ('plan_sha256','receipts','view_sha256','tables')}
            name=case['case']
            if name not in signatures:signatures[name]=expected
            assert signatures[name]==expected,('Business equivalence failed',run_data['variant'],run_data['trial'],name)
    summary={}
    for name in signatures:
        summary[name]={}
        for variant in ('old','new'):
            rows=[c for r in measurements if r['variant']==variant for c in r['cases'] if c['case']==name]
            keys=rows[0]['times'];summary[name][variant]={k:statistics.median(r['times'][k] for r in rows) for k in keys}
            summary[name][variant].update(peak_rss_mib=statistics.median(r['peak_rss_mib'] for r in rows),process_read_mib=statistics.median(r['process_read_mib'] for r in rows),process_write_mib=statistics.median(r['process_write_mib'] for r in rows))
    report={'passed':True,'synthetic':True,'repeats':a.repeats,'summary':summary,'runs':measurements,'business_equivalence':{'plans':True,'receipts':True,'ordered_tables':True,'views':True},
            'notes':'Old/new alternate serially in independent processes. Fixed synthetic clocks. Unchanged cases use the real archived recheck API. Windows process IO includes cache; RSS is sampled per worker, not whole app/PC. Warm list includes new-version summary reads. Reproducible transient DB copies are cleaned only after signed evidence is saved; inputs and first-trial fact dumps remain.'}
    write(a.out/'report.json',report);print(json.dumps({'passed':True,'summary':summary}),flush=True)


if __name__=='__main__':main()
