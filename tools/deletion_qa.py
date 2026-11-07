"""Bounded, sequential deletion comparison. Never executes against user settings."""
import argparse,hashlib,json,os,shutil,sqlite3,statistics,subprocess,sys,time,zipfile
from pathlib import Path
from contextlib import closing
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/deletion_0321'
BASE='2b7a811d72b050e2a6c6c3180aa4a162b53ccda9'
def save(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2),'utf-8',newline='\n')
def sha(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1048576),b''):h.update(b)
    return h.hexdigest()
def freeze():
    QA.mkdir(exist_ok=False);archive=QA/'baseline.zip'
    subprocess.run(['git','archive','-o',str(archive),BASE,'src','tests'],cwd=ROOT,check=True)
    with zipfile.ZipFile(archive) as z:z.extractall(QA/'baseline')
    protected=[ROOT/'交付_0.32.0_rc1/补货助手/补货助手.exe',ROOT/'交付_0.32.0_rc1/补货助手_0.32.0_rc1_参数准入与补货计划.zip']
    protected+=list((ROOT/'qa/delete_diagnosis_20261103/frozen').rglob('*'))
    save(QA/'baseline.json',{'commit':BASE,'archive':sha(archive),'protected':{str(p):sha(p) for p in protected if p.is_file()},'core':{n:sha(ROOT/'src'/n) for n in ('model.py','allocation.py','one_box_exception.py','default_config.json')}})

def signature(path):
    import storage_chunks as chunks,work_repository as wr,zlib
    records={}
    with closing(sqlite3.connect(path)) as db:
        for table, in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
            if table in ('snapshot_chunks','maintenance_chunk_refs','maintenance_legacy_refs','snapshot_storage_meta'):continue
            cols=[x[1] for x in db.execute('PRAGMA table_info("'+table+'")')];rows=[]
            for row in db.execute('SELECT * FROM "'+table+'" ORDER BY rowid'):
                values=[]
                for col,v in zip(cols,row):
                    if table=='jobs' and col in ('revision','saved'):v='<dynamic '+col+'>'
                    if isinstance(v,bytes):
                        if col=='payload' and table in ('jobs','normal_revisions','normal_work_revisions'):
                            manifest=chunks.manifest(v)
                            if manifest:logical=manifest['digest']
                            else:
                                decompressor=zlib.decompressobj();digest=hashlib.sha256()
                                for offset in range(0,len(v),65536):
                                    data=v[offset:offset+65536]
                                    while data:
                                        digest.update(decompressor.decompress(data,65536));data=decompressor.unconsumed_tail
                                if not decompressor.eof:raise ValueError('Truncated historical QA snapshot')
                                logical=digest.hexdigest()
                            v=['logical-json-sha256',logical]
                        else:v=['blob-sha256',hashlib.sha256(v).hexdigest()]
                    values.append([type(v).__name__,v])
                rows.append(values)
            records[table]=hashlib.sha256(wr.dumps([cols,rows]).encode()).hexdigest()
    return records

def worker(a):
    sys.path.insert(0,str(a.source/'src'));import data_maintenance as m
    # QA stream reader is not part of the baseline implementation under test.
    sys.path.append(str(ROOT/'src'))
    out=a.output.resolve()
    if not out.is_relative_to(QA.resolve()):raise ValueError('QA output outside isolated root')
    src=ROOT/('qa/delete_diagnosis_20261103/frozen' if a.fixture=='large' else 'qa/governance_032/delivery-final-r3/candidate-1/settings')
    settings=out/'settings';shutil.copytree(src,settings);path=settings/'workbench.sqlite3'
    if a.warm_cache:
        with closing(sqlite3.connect(path)) as db,closing(sqlite3.connect(a.warm_cache)) as warm,db:
            import maintenance_storage as ms
            ms.cache_schema(db)
            for table in ('maintenance_chunk_refs','maintenance_legacy_refs'):
                rows=warm.execute('SELECT * FROM '+table)
                db.executemany('INSERT INTO '+table+' VALUES('+','.join('?' for _ in rows.description)+')',rows)
    before=signature(path)
    with closing(sqlite3.connect(path)) as db:
        heads=[dict(oid=o,revision=v,digest=d) for o,v,d in db.execute('SELECT n.oid,n.current,r.digest FROM normal_orders n JOIN normal_revisions r ON n.oid=r.oid AND n.current=r.revision ORDER BY n.day DESC,n.rowid DESC')]
    start=time.perf_counter();stats={};stages={};last=[0.0]
    def progress(text):
        now=time.perf_counter()
        if now-last[0]>.2 or hasattr(text,'active'):
            with (out/'progress.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps({'t':now-start,'text':str(text)},ensure_ascii=False)+'\n')
            last[0]=now
    def timer(attr):
        original=getattr(m,attr);stages[attr]={'seconds':0,'calls':0}
        def timed(*args,**kw):
            tick=time.perf_counter()
            try:return original(*args,**kw)
            finally:stages[attr]['seconds']+=time.perf_counter()-tick;stages[attr]['calls']+=1;save(out/'stages-partial.json',stages)
        setattr(m,attr,timed)
    for attr in ('snapshot','strip_work','collect_shared','prepare_rewrites','apply_rewrites','clear_indexes'):
        if hasattr(m,attr):timer(attr)
    plan=None
    for head in heads[:15]:
        try:plan=m.prepare(path,[head],progress=progress);break
        except ValueError:continue
    if plan is None:raise ValueError('No eligible isolated target')
    stats['prepare']=time.perf_counter()-start;tick=time.perf_counter()
    m.execute(path,plan,True,progress=progress)
    stats['execute']=time.perf_counter()-tick;stats['total']=time.perf_counter()-start
    save(out/'result.json',{'times':stats,'stages_inclusive':stages,'signature':signature(path),'before':before,'selected_parent_count':len(plan['parents'])})

def run(a):
    sys.path.insert(0,str(ROOT/'qa/export_071/qa-deps'));import psutil
    out=QA/a.name;out.mkdir(exist_ok=False);records=[]
    for round in range(a.rounds):
        variants=['baseline','candidate'] if round%2==0 else ['candidate','baseline']
        if a.only:variants=[a.only]
        for variant in variants:
            target=out/f'{variant}-{round+1}';target.mkdir();source=QA/'baseline' if variant=='baseline' else ROOT
            cmd=[sys.executable,__file__,'worker','--source',str(source),'--output',str(target),'--fixture',a.fixture]
            if a.warm_cache:cmd+=['--warm-cache',str(a.warm_cache)]
            with (target/'process.txt').open('w',encoding='utf-8') as log:
                process=subprocess.Popen(cmd,stdout=log,stderr=log,env={**os.environ,'PYTHONIOENCODING':'utf-8'})
                root=psutil.Process(process.pid);start=time.perf_counter();peak=0;reason=None
                while process.poll() is None:
                    try:
                        children=root.children(recursive=True);rss=sum(p.memory_info().rss for p in [root,*children] if p.is_running());peak=max(peak,rss)
                    except psutil.Error:pass
                    if peak>3*1024**3 or time.perf_counter()-start>600:
                        reason='memory_cap_3GiB' if peak>3*1024**3 else 'time_cap_600s'
                        for child in reversed(children):
                            try:child.kill()
                            except psutil.Error:pass
                        process.kill();break
                    time.sleep(.05)
                code=process.wait()
            result=json.loads((target/'result.json').read_text('utf-8')) if (target/'result.json').exists() else None
            record={'variant':variant,'round':round+1,'exit':code,'stop_reason':reason,'rss_peak_sum':peak,'process_elapsed':time.perf_counter()-start,'result':result}
            records.append(record);save(out/'report.json',{'fixture':a.fixture,'records':records,'scope':'50ms parent+children RSS sum; shared pages may double-count. Process elapsed includes copy/start/import; result.times includes prepare and deletion only. No cache flush. Caps are not successful timings.'})
            print(json.dumps({k:v for k,v in record.items() if k!='result'}),flush=True)
    completed={v:[r['result']['times']['total'] for r in records if r['variant']==v and r['result']] for v in ('baseline','candidate')}
    print(json.dumps({v:{'all':x,'median':statistics.median(x)} for v,x in completed.items() if x}))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['freeze','worker','run']);p.add_argument('--name');p.add_argument('--fixture',choices=['small','large'],default='small');p.add_argument('--rounds',type=int,default=3);p.add_argument('--only',choices=['candidate','baseline']);p.add_argument('--warm-cache',type=Path);p.add_argument('--source',type=Path);p.add_argument('--output',type=Path);a=p.parse_args()
    freeze() if a.mode=='freeze' else worker(a) if a.mode=='worker' else run(a)
