"""Isolated deletion load comparison; never accepts a user database for mutation."""
import argparse,hashlib,json,os,random,shutil,sqlite3,statistics,subprocess,sys,threading,time,zipfile
from pathlib import Path
from contextlib import closing
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/deletion_0451'

def save(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2),'utf-8')
def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def freeze():
    QA.mkdir(exist_ok=False);commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    subprocess.run(['git','archive','-o',str(QA/'baseline.zip'),commit,'src','tests'],cwd=ROOT,check=True)
    with zipfile.ZipFile(QA/'baseline.zip') as z:z.extractall(QA/'baseline')
    files=[ROOT/'src'/n for n in ('model.py','allocation.py','one_box_exception.py','engine.py','default_config.json','default_must_sell.json','normal_exports.py','jst_orders.py')]
    files += [ROOT/'交付_0.45.0_rc1/补货助手/补货助手.exe',ROOT/'交付_0.45.0_rc1/补货助手_0.45.0_rc1_货盘结构分析.zip']
    save(QA/'frozen.json',{'commit':commit,'archive_sha256':sha(QA/'baseline.zip'),'protected':{str(p):sha(p) for p in files}})

def fixture():
    sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
    import test_archive_orders as f,work_repository as wr,normal_orders as no,storage_chunks as c,copy
    out=QA/'synthetic';out.mkdir();work,repo,p=f.seed(out,chunked=True);rng=random.Random(451)
    with closing(work.connect()) as db,db:
        # Many immutable revisions on one day expose compressed-history retention.
        states={s:{'name':s,'state':'待计算'} for s in ('T','U')}
        records=[({'B':'T' if i%2==0 else 'U','E':str(i)}, {'source_row':i+2,'raw':rng.randbytes(150).hex()}) for i in range(8000)]
        for rev in range(6):
            day={'schema':1,'day':wr.today(),'model_version':'0.3.4-rules','states':states,'results':{},'input_dirty':True,'loaded':{'stores':{'T':'T','U':'U'},'normalized':records},'revision_note':str(rev)}
            db.execute('INSERT INTO normal_work_revisions VALUES(?,?,?)',(f'rev-{rev}',wr.today(),no.encode(day)))
            row=copy.deepcopy(p);row['work_revision']=f'rev-{rev}';row['result']['store']='U';row['result']['store_name']='未选店'
            for d in row['result']['rows']:d['B']='U'
            oid=f'other-{rev}';digest=no.fingerprint(row['result'])
            db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(f'2026-10-{rev+1:02d}','U','normal_model',oid,oid,1))
            db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',(oid,1,digest,wr.now(),c.put(db,c.prepare(row,binding=digest))))
        db.execute('CREATE TABLE jst_runs(runid TEXT PRIMARY KEY,payload TEXT,digest TEXT)')
        for n in range(40):
            text=wr.dumps({'plans':[],'skipped':[],'trace':rng.randbytes(256*1024).hex()})
            db.execute('INSERT INTO jst_runs VALUES(?,?,?)',(str(n),text,hashlib.sha256(text.encode()).hexdigest()))
        # Shared chunked payload creates a real live-reference scan, including cold cache.
        row={'states':states,'loaded':{'normalized':records},'revision_note':'chunked'}
        db.execute('INSERT INTO normal_work_revisions VALUES(?,?,?)',('frozen-chunked',wr.today(),c.put(db,c.prepare(row))))
    save(out/'fixture.json',{'synthetic':True,'selected_parent':'oid','legacy_revisions':6,'records_per_revision':8000,'run_records':40,'files':{str(p.relative_to(out)):sha(p) for p in out.rglob('*.sqlite3')}})

def signature(folder):
    import deletion_qa
    return {p.name:deletion_qa.signature(p) for p in sorted(folder.rglob('*.sqlite3')) if 'prepared' not in p.name}

def worker(a):
    sys.path[:0]=[str(a.source/'src'),str(ROOT/'qa/export_071/qa-deps')]
    import psutil,data_maintenance as m,maintenance_storage as ms
    # QA helper for streaming legacy signatures, never substituted for the implementation.
    out=a.output.resolve();out.relative_to(a.qa_root.resolve());out.mkdir(parents=True,exist_ok=False)
    shutil.copytree(a.fixture,out/'settings');path=out/'settings/workbench.sqlite3'
    if a.warm:
        with closing(sqlite3.connect(path)) as db,closing(sqlite3.connect(a.warm)) as warm,db:
            ms.cache_schema(db)
            for table in ('maintenance_chunk_refs','maintenance_legacy_refs'):
                rows=warm.execute('SELECT * FROM '+table)
                db.executemany('INSERT OR REPLACE INTO '+table+' VALUES('+','.join('?' for _ in rows.description)+')',rows)
    before=signature(out/'settings');stages={};events=[];proc=psutil.Process();stop=threading.Event();peak=[0];calls={'chunk_bytes':0,'chunk_calls':0}
    scratch_bytes=None
    def sample():
        while not stop.wait(.03):peak[0]=max(peak[0],proc.memory_info().rss)
    def timed(name):
        fn=getattr(m,name);stages[name]={'seconds':0,'calls':0}
        def run(*args,**kwargs):
            nonlocal scratch_bytes
            start=time.perf_counter()
            try:
                result=fn(*args,**kwargs)
                if name=='prepare_rewrites':
                    scratch_path=args[2].execute('PRAGMA database_list').fetchone()[2]
                    scratch_bytes=Path(scratch_path).stat().st_size
                return result
            finally:stages[name]['seconds']+=time.perf_counter()-start;stages[name]['calls']+=1
        setattr(m,name,run)
    for name in ('snapshot','prepare_rewrites','apply_rewrites','collect_shared','clear_indexes'):timed(name)
    original=ms.references_for
    def refs(db,h,size,data,*args,**kwargs):
        calls['chunk_bytes']+=len(data);calls['chunk_calls']+=1
        return original(db,h,size,data,*args,**kwargs)
    ms.references_for=refs
    def progress(s):
        if len(events)<250 or getattr(s,'active',False):events.append({'seconds':time.perf_counter()-start,'text':str(s)})
    with closing(sqlite3.connect(path)) as db:
        candidates=list(db.execute('SELECT n.oid,n.current,r.digest,n.day FROM normal_orders n JOIN normal_revisions r ON n.oid=r.oid AND n.current=r.revision ORDER BY n.day DESC,n.rowid DESC'))
    heads=[dict(oid=o,revision=r,digest=d) for o,r,d,_ in candidates]
    if a.parents>1:
        by_day={}
        for head,(*_,day) in zip(heads,candidates):by_day.setdefault(day,head)
        if len(by_day)<a.parents:raise ValueError('Not enough distinct days in isolated fixture')
        selections=[list(by_day.values())[:a.parents]]
    else:
        selections=[[head] for head in heads[:15]]
    start=time.perf_counter();io=proc.io_counters();thread=threading.Thread(target=sample);thread.start();plan=None
    try:
        for selection in selections:
            try:plan=m.prepare(path,selection,progress=progress);break
            except ValueError:
                if a.parents>1:raise
        if plan is None:raise ValueError('No eligible isolated target')
        preview=time.perf_counter()-start;token=plan['token'];tick=time.perf_counter()
        # Same clock for the deletion event in both implementations. No event
        # fields are ignored in ordered business comparison.
        from unittest.mock import patch
        with patch.object(m.time,'time',return_value=1793923200.0):result=m.execute(path,plan,True,progress=progress)
        execute=time.perf_counter()-tick
    finally:stop.set();thread.join()
    endio=proc.io_counters();total=time.perf_counter()-start;after=signature(out/'settings')
    save(out/'result.json',{'preview_seconds':preview,'execute_seconds':execute,'total_seconds':total,'peak_rss':peak[0],
        'io':{k:getattr(endio,k)-getattr(io,k) for k in io._fields},'stages':stages,'scan':calls,'scratch_prepared_bytes':scratch_bytes,'plan_token':token,
        'result':result,'before':before,'after':after,'progress':events})

def run(a):
    out=a.output;out.resolve().relative_to(a.qa_root.resolve());out.mkdir(parents=True,exist_ok=False)
    records=[];fixture_hash={str(p):sha(p) for p in a.fixture.rglob('*') if p.is_file()}
    if a.warm:fixture_hash[str(a.warm)]=sha(a.warm)
    for n in range(a.rounds):
        for side in (('before','after') if n%2==0 else ('after','before')):
            dest=out/f'{side}-{n+1}';source=a.qa_root/'baseline' if side=='before' else ROOT
            cmd=[sys.executable,__file__,'worker','--source',str(source),'--fixture',str(a.fixture),'--output',str(dest),
                 '--qa-root',str(a.qa_root),'--parents',str(a.parents)]
            if a.warm:cmd+=['--warm',str(a.warm)]
            with (out/f'{side}-{n+1}.txt').open('w',encoding='utf-8') as f:subprocess.run(cmd,cwd=ROOT,stdout=f,stderr=f,env={**os.environ,'PYTHONIOENCODING':'utf-8','PYTHONHASHSEED':'0'},check=True,timeout=900)
            r=json.loads((dest/'result.json').read_text('utf-8'));records.append({'side':side,'round':n+1,**r})
            save(out/'partial.json',records);print(json.dumps({'side':side,'round':n+1,'seconds':r['total_seconds'],'rss_mib':r['peak_rss']/2**20}),flush=True)
    old=[r for r in records if r['side']=='before'];new=[r for r in records if r['side']=='after'];diff=[]
    for i,(b,c) in enumerate(zip(old,new)):
        for k in ('before','after','result','plan_token'):
            if b[k]!=c[k]:diff.append({'round':i+1,'field':k})
    summary={}
    for side in ('before','after'):
        rs=[r for r in records if r['side']==side];summary[side]={}
        for k in ('preview_seconds','execute_seconds','total_seconds','peak_rss'):
            vs=[r[k] for r in rs];summary[side][k]={'median':statistics.median(vs),'min':min(vs),'max':max(vs)}
        for k in ('read_bytes','write_bytes'):
            vs=[r['io'][k] for r in rs];summary[side][k]={'median':statistics.median(vs),'min':min(vs),'max':max(vs)}
    protected=json.loads((a.qa_root/'frozen.json').read_text('utf-8'))['protected'];changed=[p for p,h in {**fixture_hash,**protected}.items() if sha(Path(p))!=h]
    report={'passed':not diff and not changed,'summary':summary,'differences':diff,'changed_protected':changed,'runs':records,
        'scope':'Alternating serial runs; warm OS cache; 30ms worker RSS; Windows process I/O counts API bytes, not physical disk activity. Timed preview+delete, excludes fixture copy/signature. Exact plan token/result and ordered per-table typed signature; only jobs.revision/saved are dynamic; deletion event time frozen to 1793923200.0 on both sides, no ignored event fields; internal chunk/cache layout excluded by existing deletion oracle.'}
    save(out/'report.json',report)
    if not report['passed']:raise AssertionError(str(diff or changed))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['freeze','fixture','worker','run']);p.add_argument('--source',type=Path);p.add_argument('--fixture',type=Path);p.add_argument('--output',type=Path);p.add_argument('--warm',type=Path);p.add_argument('--rounds',type=int,default=3)
    p.add_argument('--qa-root',type=Path,default=QA,help='Independent comparison root with baseline/src and frozen.json')
    p.add_argument('--parents',type=int,default=1,help='Select this many parents, from distinct days for multiple parents')
    a=p.parse_args();a.qa_root.resolve().relative_to(ROOT/'qa')
    if a.parents<1 or a.rounds<1:p.error('parents and rounds must be positive')
    globals()[a.mode](a) if a.mode in ('worker','run') else globals()[a.mode]()
