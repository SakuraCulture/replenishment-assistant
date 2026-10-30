"""Alternating isolated 0.7.4/0.8.0 full five-store action measurements.

Child timing includes spawn, IPC, review preparation, commit and exports. Profile
wrappers observe both versions without altering results. Run one child at a time.
"""
import argparse,hashlib,json,os,statistics,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def profiled_serve(connection,path,cancel):
    sys.path.insert(0,str(Path(os.environ['IO_QA_SOURCE'])/'src'))
    import work_repository as wr,normal_orders as no,work_io_process as wi,zlib
    counters={}
    def wrap(obj,name,key):
        original=getattr(obj,name)
        def call(*args,**kwargs):
            start=time.perf_counter()
            try:return original(*args,**kwargs)
            finally:
                row=counters.setdefault(key,{'calls':0,'seconds':0});row['calls']+=1;row['seconds']+=time.perf_counter()-start
        setattr(obj,name,call)
    for obj,name,key in [(wr,'dumps','json_encode'),(wr,'loads','json_decode'),(zlib,'compress','zlib_compress'),(zlib,'decompress','zlib_decompress'),(no,'fingerprint','fingerprint_copy'),(no,'fingerprint_owned','fingerprint_owned'),(wr.Repository,'get_job','get_job'),(no.Repository,'get','get_order')]:wrap(obj,name,key)
    send=wi.send_tree
    def measured_send(*args,**kwargs):
        Path(os.environ['IO_QA_PROFILE']).write_text(json.dumps(counters,indent=2),encoding='utf-8')
        return send(*args,**kwargs)
    wi.send_tree=measured_send
    try:wi.serve(connection,path,cancel)
    finally:
        Path(os.environ['IO_QA_PROFILE']).write_text(json.dumps(counters,indent=2),encoding='utf-8')


def storage_serve(connection,path,cancel):
    sys.path.insert(0,str(Path(os.environ['IO_QA_SOURCE'])/'src'))
    import normal_orders,work_io_process
    normal_orders.MODEL_VERSION='0.3.0-rules'
    work_io_process.serve(connection,path,cancel)

def child(source,out,snapshot):
    os.environ['REPLENISHMENT_DATA_DIR']=str(out/'settings');os.environ['IO_QA_SOURCE']=str(source)
    sys.path.insert(0,str(source/'src'))
    import sqlite3,work_repository as wr,normal_orders as no,new_product_focus as focus,normal_delivery as nd,ui_work_io as ui
    profiling=os.environ.get('IO_QA_PROFILE_ENABLED')=='1'
    if profiling:ui.serve=profiled_serve
    else:ui.serve=storage_serve
    ro=wr.Repository.__new__(wr.Repository);ro.path=snapshot
    ro.connect=lambda:sqlite3.connect(snapshot.as_uri()+'?mode=ro',uri=True)
    p=ro.get_job(ro.list_jobs()[0]['day'])['payload'];p['day']=wr.today();p['new_focus']=focus.empty(wr.today()[:7]);p['new_focus']['saved_at']=p['day']+'T00:00:00+08:00'
    # The frozen business payload is identical on both sides (including app version).
    # Storage-only comparison holds the fixture's business model identity fixed on both sides.
    no.MODEL_VERSION=p['model_version']
    work=wr.Repository(out/'settings');repo=no.Repository(work);codes=list(p['results']);report={'passed':False,'source':str(source),'timings':{},'profiles':{},'source_hashes':{f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in (source/'src').glob('*') if f.is_file()}}
    def measure(name,action):
        (out/'phase.txt').write_text(name,encoding='ascii');os.environ['IO_QA_PROFILE']=str(out/(name+'_profile.json'))
        os.environ['REPLENISHMENT_IO_TRACE']=str(out/'stages'/name)
        start=time.perf_counter();messages=[]
        with ui.Session(work.path) as session:action(session,lambda text:messages.append((str(text),getattr(text,'current',None),getattr(text,'total',None))))
        report['timings'][name]=time.perf_counter()-start
        report['profiles'][name]=json.loads((out/(name+'_profile.json')).read_text()) if profiling else {}
        (out/(name+'_progress.json')).write_text(json.dumps(messages,ensure_ascii=False),encoding='utf-8')
    def save(session,progress):
        prepared=session.call('prepare_save',p,progress)
        if not prepared['same']:session.call('save',None,progress)
    measure('save',save)
    measure('load',lambda s,pr:s.call('load',p['day'],pr))
    measure('copy_config',lambda s,pr:s.call('copy_config',p['day'],pr))
    measure('save_unchanged',save)
    p['export_log'].append({'qa_stage1':'cancel only'})
    measure('overwrite_cancel',lambda s,pr:s.call('prepare_save',p,pr))
    p['export_log'].pop()
    def sign(s,pr):s.call('prepare_normal',[p,codes],pr);s.call('confirm_normal',None,pr)
    measure('confirm',sign)
    measure('confirm_unchanged',sign)
    export=out/'merchant';export.mkdir()
    def deliver(s,pr):
        if (source/'src/io_stages.py').exists():
            slim={k:p[k] for k in ('day','input_dirty','new_focus','model_version')};slim.update(states={c:p['states'][c] for c in codes},results={c:p['results'][c] for c in codes},_draft_summary=wr.summary(p))
            plan=s.call('delivery_probe',[slim,codes],pr);assert all(item['same'] for item in plan['plans'])
        else:s.call('delivery_prepare',[p,codes],pr)
        s.call('delivery_execute',str(export),pr)
    measure('export_signed',deliver)
    (out/'phase.txt').write_text('validate',encoding='ascii')
    saved=work.get_job(p['day']);expected=hashlib.sha256(wr.dumps(p).encode()).hexdigest();assert saved['digest']==expected
    report['payload_digest']=expected;report['orders']=[]
    for code in codes:
        order=repo.get(p['day'],code);assert order['digest']==no.fingerprint_owned(p['results'][code],p['new_focus'])
        report['orders'].append({'store':code,'rows':len(order['payload']['result']['rows']),'digest':order['digest'],'revision':order['revision'],'number':order['number']})
    with work.connect() as db:report['schema']=list(db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND name NOT IN ('snapshot_chunks','snapshot_storage_meta') ORDER BY name"))
    report['database_bytes']=work.path.stat().st_size
    report.update(passed=True,files=[x.name for x in export.glob('*.xlsx')]);(out/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


def run_one(source,out,snapshot):
    out.mkdir(parents=True,exist_ok=False);sys.path.insert(0,str(ROOT/'qa/export_071/qa-deps'));import psutil
    env=os.environ.copy();env['PYTHONHASHSEED']='0';env['PYTHONIOENCODING']='utf-8'
    with (out/'run.log').open('w',encoding='utf-8') as log:
        proc=subprocess.Popen([sys.executable,__file__,'--child','--source',str(source),'--output',str(out),'--snapshot',str(snapshot)],env=env,stdout=log,stderr=log);host=psutil.Process(proc.pid);peaks={};children=set();start=time.monotonic()
        while proc.poll() is None:
            if time.monotonic()-start>900:
                for c in host.children(recursive=True):c.kill()
                host.kill();raise TimeoutError(str(out))
            try:
                phase=(out/'phase.txt').read_text() if (out/'phase.txt').exists() else 'startup';tree=host.children(recursive=True);children.update((c.pid,c.create_time()) for c in tree)
                peaks[phase]=max(peaks.get(phase,0),sum(c.memory_info().rss for c in [host]+tree))
            except (psutil.Error,OSError):pass
            time.sleep(.05)
    assert proc.returncode==0,(out,proc.returncode)
    left=[]
    for pid,created in children:
        try:
            c=psutil.Process(pid)
            if c.create_time()==created and c.is_running():left.append(pid)
        except psutil.Error:pass
    result=json.loads((out/'result.json').read_text(encoding='utf-8'));result.update(peak_tree_mib={k:v/2**20 for k,v in peaks.items()},remaining_children=left);assert not left
    (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');return result


def main():
    p=argparse.ArgumentParser();p.add_argument('--child',action='store_true');p.add_argument('--profile',action='store_true');p.add_argument('--source',type=Path,default=ROOT);p.add_argument('--baseline',type=Path,default=ROOT/'qa/storage_080/baseline');p.add_argument('--output',type=Path,required=True);p.add_argument('--snapshot',type=Path,default=ROOT/'qa/archive_072/snapshot.sqlite3');p.add_argument('--runs',type=int,default=3);a=p.parse_args()
    if a.child:return child(a.source.resolve(),a.output.resolve(),a.snapshot.resolve())
    os.environ['IO_QA_PROFILE_ENABLED']='1' if a.profile else '0'
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);results={'old':[],'new':[]}
    for i in range(a.runs):
        for kind in (['old','new'] if i%2==0 else ['new','old']):
            r=run_one(a.baseline.resolve() if kind=='old' else ROOT,out/f'{kind}-{i}',a.snapshot.resolve());results[kind].append(r)
            print(kind,str(i),json.dumps(r['timings']),flush=True)
    reference=results['old'][0]
    for values in results.values():
        for r in values:
            for field in ['payload_digest','orders','schema','files']:assert r[field]==reference[field],field
    summary={}
    for phase in reference['timings']:
        summary[phase]={}
        for kind in results:
            ts=[r['timings'][phase] for r in results[kind]];ms=[r['peak_tree_mib'].get(phase,0) for r in results[kind]]
            summary[phase][kind]={'median':statistics.median(ts),'min':min(ts),'max':max(ts),'peak_rss_mib_median':statistics.median(ms)}
    report={'passed':True,'runs':a.runs,'database_bytes':{k:[v['database_bytes'] for v in values] for k,values in results.items()},'summary':summary,'identity':['full canonical daily digest','per-store business fingerprint','original schema (added storage tables excluded)','filenames','revision and number'],'memory':'external 50ms sum main+child RSS; shared pages counted per process; validation separate','cache':'OS file cache not purged; alternating old/new then new/old, sequential execution','profile':'inclusive nested function timings are not additive; full session times include spawn, transfer, review, commit and file write'}
    import platform,importlib.metadata,psutil
    report['environment']={'python':sys.version,'executable':sys.executable,'platform':platform.platform(),'logical_cpus':psutil.cpu_count(),'physical_cpus':psutil.cpu_count(logical=False),'physical_memory_mib':psutil.virtual_memory().total/2**20,'packages':{name:importlib.metadata.version(name) for name in ['PySide6-Essentials','openpyxl','PyInstaller']},'snapshot_sha256':hashlib.sha256(a.snapshot.read_bytes()).hexdigest()}
    (out/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps(report),flush=True)

if __name__=='__main__':
    import multiprocessing
    multiprocessing.freeze_support();main()
