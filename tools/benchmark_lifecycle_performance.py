"""Alternating baseline/candidate benchmark; exact synthetic facts and process RSS."""
import argparse,copy,gc,gzip,hashlib,json,os,shutil,sqlite3,statistics,subprocess,sys,threading,time,uuid
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
CORE=('order_lifecycle.py','lifecycle_batch.py','lifecycle_read_proofs.py','lifecycle_save_encoding.py','jst_orders.py','jst_batches.py','jst_allocation.py','lifecycle_rules.py','lifecycle_io.py','purchases.py','work_repository.py','warehouse_orders.py','warehouse_execution.py','payment_matching.py')
def write(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n','utf-8',newline='\n')
def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def clone(source,target):
    assert source.is_file() and not target.exists()
    with closing(sqlite3.connect(source)) as src,closing(sqlite3.connect(target)) as dst:src.backup(dst)

def worker(a):
    sys.path[:0]=[str(a.source/'src'),str(a.deps)]
    import psutil,work_repository as wr,order_lifecycle as lc,lifecycle_batch as batch,purchases
    import jst_orders as jo,jst_batches as jb,warehouse_catalog as wc
    from unittest.mock import patch
    assert json.loads((a.fixture/'synthetic.json').read_text('utf-8'))['synthetic']
    a.out.mkdir(parents=True,exist_ok=False);hashes={n:sha(a.source/'src'/n) for n in CORE};process=psutil.Process();cases=[]
    scans={s:batch.read([a.fixture/(s+'.xlsx')],s)['documents'] for s in ('payment','receipt')}
    assert all(len(d)==6 for d in scans.values())
    def facts(dbpath,dump=None):
        result={};stream=gzip.open(dump,'wt',encoding='utf-8') if dump else None
        try:
            with closing(sqlite3.connect(dbpath)) as db:
                assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
                for name,definition in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
                    count=0;digest=hashlib.sha256()
                    for row in db.execute('SELECT * FROM "'+name+'" ORDER BY rowid'):
                        values=[{'blob':v.hex()} if isinstance(v,bytes) else v for v in row];text=wr.dumps(values)
                        digest.update(text.encode());digest.update(b'\n');count+=1
                        if stream:stream.write(wr.dumps([name,values])+'\n')
                    result[name]={'rows':count,'sha256':digest.hexdigest(),'definition':definition}
        finally:
            if stream:stream.close()
        return result
    def business_plan(plan):
        return {**plan,'plans':[{k:v for k,v in p.items() if k!='_read_proofs'} for p in plan['plans']]} if 'plans' in plan else plan
    def signature(value):return hashlib.sha256(wr.dumps(value).encode()).hexdigest()
    for case in ('payment','receipt','jst'):
        folder=a.out/case;folder.mkdir();working=folder/'isolated-db';working.mkdir();db=working/'workbench.sqlite3';clone(a.fixture/'complete.sqlite3',db)
        counter=[0]
        def uid():counter[0]+=1;return uuid.UUID(int=counter[0])
        rows=[]
        def measure(name,fn,*,kind=None):
            gc.collect();peak=[process.memory_info().rss];stop=threading.Event();events=[];states=[];waits=[];locks=[];first=[None];begin=lc.begin
            def sample():
                while not stop.wait(.02):peak[0]=max(peak[0],process.memory_info().rss)
            def checked_begin(*args,**kwargs):
                t=time.perf_counter();r=begin(*args,**kwargs);acquired=time.perf_counter();waits.append(acquired-t);locks.append(acquired)
                if first[0] is None:first[0]=acquired
                return r
            def progress(message):
                events.append(str(message))
                if hasattr(message,'active'):states.append(bool(message.active))
            thread=threading.Thread(target=sample);thread.start();before=process.io_counters();t=time.perf_counter()
            try:
                with patch('order_lifecycle.begin',checked_begin):value=fn(progress)
                end=time.perf_counter();after=process.io_counters()
            finally:stop.set();thread.join()
            peak[0]=max(peak[0],process.memory_info().rss)
            item={'phase':name,'seconds':end-t,'peak_rss_mib':peak[0]/2**20,'process_read_mib':(after.read_bytes-before.read_bytes)/2**20,
                  'process_write_mib':(after.write_bytes-before.write_bytes)/2**20,'write_lock_wait_seconds':sum(waits),
                  'first_write_lock_through_close_seconds':end-first[0] if first[0] is not None else 0,'transaction_count':len(locks)}
            if kind=='plan':item['plan_sha256']=signature(business_plan(value))
            else:
                item['result']=value;item['ordered_tables']=facts(db)
                item['progress_sha256']=signature(events);item['commit_states']=states
            rows.append(item);print(json.dumps({'variant':a.variant,'trial':a.trial,'case':case,**{k:item[k] for k in ('phase','seconds','peak_rss_mib')}},ensure_ascii=False),flush=True)
            return value
        with patch('uuid.uuid4',uid),patch('work_repository.now',lambda:'2026-11-15T12:00:00+08:00'):
            if case in ('payment','receipt'):
                if case=='receipt':lc.commit(db,batch.prepare(db,scans['payment']))
                plan=measure('prepare',lambda progress:batch.prepare(db,scans[case],progress=progress),kind='plan')
                result=measure('save',lambda progress:lc.commit(db,plan,progress=progress));assert len(result)==6 and all(r['changed'] for r in result)
                del plan;gc.collect()
                plan=measure('reprepare',lambda progress:batch.prepare(db,scans[case],progress=progress),kind='plan')
                result=measure('unchanged_save',lambda progress:lc.commit(db,plan,progress=progress));assert not any(r['changed'] for r in result)
            else:
                heads=purchases.targets(db);items={s:{'商品编码':s,'分类':'合成分类','基本售价':2,'出库箱规':1,'商品状态':'启用','可用数':100000,'商品名称':p['name'],'商品标签':'','_row':i+2}
                      for i,(s,p) in enumerate({s:p for h in heads for s,p in h['products'].items()}.items())}
                cat={'schema':1,'warehouse':'河北仓','items':items,'revision':wc.revision(items),'imported_at':'2026-11-15T12:00:00+08:00',
                     'source_path':str(a.fixture/'synthetic-catalog-not-a-file.xlsx'),'source_sha256':'synthetic-only','sheet':'合成资料'}
                plan=measure('preview',lambda progress:jb.preview_new(db,heads,{'河北仓':cat},progress=progress),kind='plan')
                assert not any(p['summary']['issues'] for p in plan['payload']['plans'])
                plan=measure('register',lambda progress:jb.register(db,plan,progress=progress),kind='plan')
                result=measure('save',lambda progress:jo.commit(db,plan,progress=progress));assert not result['errors'] and len(result['generated'])==6
                ids=result['generated']
                again=measure('replay_save',lambda progress:jo.commit(db,plan,progress=progress));assert again['generated']==ids and not again['errors']
                measure('lock_uncontended',lambda progress:jo.lock(db,ids,progress=progress))
        final=facts(db,folder/'business-facts.json.gz' if a.trial==1 else None)
        result={'case':case,'phases':rows,'final_ordered_tables':final};write(folder/'report.json',result);cases.append(result)
        assert working.resolve().is_relative_to(a.out.resolve()) and working.name=='isolated-db'
        shutil.rmtree(working)  # Only this new run's reproducible, signed synthetic copies.
    assert hashes=={n:sha(a.source/'src'/n) for n in CORE}
    write(a.out/'report.json',{'passed':True,'variant':a.variant,'trial':a.trial,'cases':cases,'core_source_sha256':hashes})

def main():
    p=argparse.ArgumentParser();p.add_argument('--fixture',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--baseline',type=Path);p.add_argument('--source',type=Path);p.add_argument('--deps',type=Path,required=True)
    p.add_argument('--worker',action='store_true');p.add_argument('--variant');p.add_argument('--trial',type=int,default=1);p.add_argument('--repeats',type=int,default=3);a=p.parse_args()
    if a.worker:return worker(a)
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False);fixture=a.fixture.resolve();reports=[]
    before={p.name:sha(p) for p in fixture.iterdir() if p.is_file()}
    for trial in range(1,a.repeats+1):
        for variant in (('old','new') if trial%2 else ('new','old')):
            dest=out/f'{trial}-{variant}';source=a.baseline.resolve() if variant=='old' else ROOT
            command=[sys.executable,str(Path(__file__).resolve()),'--worker','--source',str(source),'--fixture',str(fixture),'--out',str(dest),'--deps',str(a.deps.resolve()),'--variant',variant,'--trial',str(trial)]
            with (out/f'{trial}-{variant}.log').open('wb') as log:r=subprocess.run(command,stdout=log,stderr=log,env={**os.environ,'PYTHONUTF8':'1'})
            if r.returncode:raise RuntimeError('Failed isolated worker; retained '+str(log.name))
            reports.append(json.loads((dest/'report.json').read_text('utf-8')));print(f'completed {trial} {variant}',flush=True)
    expected=None
    for report in reports:
        comparison=[]
        for c in report['cases']:
            comparison.append({'case':c['case'],'final_ordered_tables':c['final_ordered_tables'],'phases':[
                {k:v for k,v in r.items() if k not in ('seconds','peak_rss_mib','process_read_mib','process_write_mib','write_lock_wait_seconds','first_write_lock_through_close_seconds','transaction_count')} for r in c['phases']]})
        if expected is None:expected=comparison
        assert comparison==expected,('Business, ordering or validation progress differs',report['variant'],report['trial'])
    summary={}
    for case in reports[0]['cases']:
        summary[case['case']]={}
        for ph in case['phases']:
            summary[case['case']][ph['phase']]={}
            for variant in ('old','new'):
                rows=[next(p for c in r['cases'] if c['case']==case['case'] for p in c['phases'] if p['phase']==ph['phase']) for r in reports if r['variant']==variant]
                summary[case['case']][ph['phase']][variant]={k:statistics.median(row[k] for row in rows) for k in ('seconds','peak_rss_mib','process_read_mib','process_write_mib','write_lock_wait_seconds','first_write_lock_through_close_seconds','transaction_count')}
    assert before=={p.name:sha(p) for p in fixture.iterdir() if p.is_file()}
    write(out/'report.json',{'passed':True,'synthetic':True,'live_platform':False,'repeats':a.repeats,'orders':6,'rows':12000,
          'business_equivalence':{'plans':True,'results':True,'ordered_tables':True,'progress_and_boundaries':True},'summary':summary,'runs':reports,'input_sha256':before,
          'notes':'Alternating independent processes, fixed synthetic UUIDs/times, 20ms worker RSS samples; Windows process IO includes system cache. Initial file parsing/copy outside timing. Timing is backend API work, not Qt rendering or list refresh. lock_uncontended is compared; short-reader behavior change is validated separately. Full first-trial facts retained before new temporary DB copies are cleaned.'})
    print(json.dumps({'passed':True,'summary':summary},ensure_ascii=False),flush=True)
if __name__=='__main__':main()
