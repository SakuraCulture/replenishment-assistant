"""Frozen-source lifecycle equivalence and serial alternating end-to-end timing."""
import argparse,hashlib,json,os,shutil,subprocess,sys,time,threading,statistics
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def save(p,x):Path(p).write_text(json.dumps(x,ensure_ascii=False,indent=2,default=str),encoding='utf-8')

def worker(source,fixture,out,mode):
    sys.path[:0]=[str(source/'src'),str(ROOT/'tools'),str(ROOT/'qa/export_071/qa-deps')]
    import lifecycle_io as io,order_lifecycle as service
    from structure_oracle import excel_snapshot
    import psutil
    out.mkdir();db=out/'workbench.sqlite3';shutil.copy2(fixture/'settings/workbench.sqlite3',db)
    specs=json.loads((fixture/'specs.json').read_text(encoding='utf-8'));heads=service.current_targets(db);by_number={h['number']:h for h in heads};hb=next(h for h in heads if h['warehouse']=='河北仓')
    peak=[0];stop=threading.Event();proc=psutil.Process()
    def sample():
        while not stop.is_set():
            try:peak[0]=max(peak[0],sum(p.memory_info().rss for p in [proc,*proc.children(recursive=True)]))
            except psutil.Error:pass
            stop.wait(.02)
    sampler=threading.Thread(target=sample);sampler.start();t=time.perf_counter();scans=[]
    for item in specs:scans.append((item['stage'],io.scan(item['stage'],item['specs'])))
    read=time.perf_counter()-t;pstart=time.perf_counter()
    if mode=='new':
        import lifecycle_batch
        docs=[]
        # Deliberately reverse arrival order: receipt before payment before return.
        for stage,scan in reversed(scans):
            for d in lifecycle_batch.group_scan(scan):
                d['target']=by_number.get(d['scan']['rows'][0]['signed_number']) or hb;docs.append(d)
        plan=lifecycle_batch.prepare(db,docs);prepared=time.perf_counter()-pstart;start=time.perf_counter();service.commit(db,plan);committed=time.perf_counter()-start
    else:
        prepared=committed=0
        for stage,scan in scans:
            start=time.perf_counter();plan=service.prepare_numbered(db,scan,hb if stage=='payment' else None);prepared+=time.perf_counter()-start
            start=time.perf_counter();service.commit(db,plan);committed+=time.perf_counter()-start
    start=time.perf_counter();values={};exports={}
    # Fix the non-business collision suffix identically in each isolated worker;
    # compare full filenames, instead of ignoring filename differences.
    import uuid,itertools
    sequence=itertools.count(1);uuid.uuid4=lambda:uuid.UUID(int=next(sequence)*2**96)
    for h in heads:
        for stage in io.STAGES:
            docs=service.history(db,h,stage)
            values[h['number']+'/'+stage]=[{k:d[k] for k in ('version','digest','payload')} for d in docs]
    for kind in (*io.STAGES,'comparison'):
        eligible=[h for h in heads if kind=='comparison' or service.history(db,h,kind)]
        folder=out/kind;folder.mkdir();result=service.export(db,eligible,folder,kind,'separate')
        if result['error'] or result['cancelled']:raise AssertionError(result)
        exports[kind]=[{'name':Path(f).name,'excel':excel_snapshot(f)} for f in result['files']]
    exported=time.perf_counter()-start;elapsed=time.perf_counter()-t;stop.set();sampler.join()
    save(out/'snapshot.json',{'documents':values,'exports':exports})
    save(out/'timing.json',{'read_seconds':read,'prepare_seconds':prepared,'commit_seconds':committed,'export_seconds':exported,'total_seconds':elapsed,'peak_tree_mib':peak[0]/2**20,'sample_ms':20})

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--baseline',type=Path,required=True);ap.add_argument('--output',type=Path,required=True);ap.add_argument('--worker',choices=['old','new']);ap.add_argument('--fixture',type=Path);a=ap.parse_args()
    if a.worker:return worker(a.baseline.resolve() if a.worker=='old' else ROOT,a.fixture.resolve(),a.output.resolve(),a.worker)
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);fixture=out/'fixture';fixture.mkdir()
    sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tools'),str(ROOT/'tests')]
    from verify_reconciliation_exe import fixture as make_fixture
    import lifecycle_io as io
    import lifecycle_batch
    from parallel_qa import diff
    make_fixture(fixture);specs=[]
    for stage,files in [('return',['return-hb.xlsx','return-sd.xlsx']),('payment',['payment.xlsx']),('receipt',['receipt.xlsx'])]:
        s=[]
        for f in files:
            info=io.inspect([fixture/f])[0];auto,unknown=lifecycle_batch.automatic_specs(info,stage)
            if unknown:raise AssertionError(unknown)
            s.extend(p for _,p in auto)
        specs.append({'stage':stage,'specs':s})
    save(fixture/'specs.json',specs);hashes={str(p):sha(p) for p in fixture.rglob('*') if p.is_file()}
    runs={'old':[],'new':[]};changes=[]
    env=os.environ.copy();env['PYTHONIOENCODING']='utf-8'
    for i in range(3):
        for mode in (['old','new'] if i%2==0 else ['new','old']):
            target=out/f'{mode}-{i}'
            subprocess.run([sys.executable,__file__,'--baseline',str(a.baseline.resolve()),'--output',str(target),'--fixture',str(fixture),'--worker',mode],check=True,env=env)
            runs[mode].append(json.loads((target/'timing.json').read_text(encoding='utf-8')))
        old,new=[json.loads((out/f'{mode}-{i}/snapshot.json').read_text(encoding='utf-8')) for mode in ('old','new')]
        changes.extend(diff(old,new))
    summary={mode:{key:{'median':statistics.median(r[key] for r in rr),'min':min(r[key] for r in rr),'max':max(r[key] for r in rr)} for key in rr[0]} for mode,rr in runs.items()}
    drift=[p for p,h in hashes.items() if sha(p)!=h]
    report={'passed':not changes and not drift,'differences':changes,'changed_inputs':drift,'inputs':hashes,'baseline_source':str(a.baseline.resolve()),'baseline_commit':'3e167f5c86f64d1d8a98adffda8287b35ab76361','runs':runs,'summary':summary,'whitelist':['document envelope saved timestamp only','Excel exact build/clock cells in structure_oracle.excel_snapshot'], 'scope':'Exact payload/digest/version/source-row/order and complete existing lifecycle Excel cell/style snapshots; batch file arrival deliberately reversed. Synthetic two warehouses, four source files, four signed products; same frozen originals and interpreter; alternating serial runs, OS cache uncontrolled, no user reading/confirmation time.'}
    save(out/'report.json',report);print(json.dumps({'passed':report['passed'],'differences':len(changes),'summary':summary}))
    if not report['passed']:raise AssertionError('batch differs from fixed sequential baseline')
if __name__=='__main__':main()
