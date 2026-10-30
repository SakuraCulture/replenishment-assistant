"""Alternating full five-store prepare/preview/archive/export measurements, spawn included."""
import argparse,hashlib,json,os,statistics,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def child(source,snapshot,out):
    source_hashes={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in (source/'src').glob('*') if f.is_file()}
    sys.path.insert(0,str(source/'src'));os.environ['REPLENISHMENT_DATA_DIR']=str(out/'settings')
    import sqlite3,work_repository as wr,normal_orders as no,new_product_focus as focus
    from ui_work_io import Session
    ro=wr.Repository.__new__(wr.Repository);ro.path=snapshot;ro.connect=lambda:sqlite3.connect(snapshot.as_uri()+'?mode=ro',uri=True)
    p=ro.get_job(ro.list_jobs()[0]['day'])['payload'];p['day']=wr.today();p['new_focus']=focus.empty(p['day'][:7]);p['new_focus']['saved_at']=p['day']+'T00:00:00+08:00'
    work=wr.Repository(out/'settings');repo=no.Repository(work);codes=list(p['results']);timings={};messages={}
    def measure(name,fn):
        (out/'phase.txt').write_text(name);start=time.perf_counter();log=[]
        with Session(work.path) as s:fn(s,lambda m:log.append({'at':time.perf_counter()-start,'message':str(m)}))
        timings[name]=time.perf_counter()-start;messages[name]=log
    measure('prepare_unsigned',lambda s,pr:s.call('delivery_prepare',[p,codes],pr))
    def sign(s,pr):
        start=time.perf_counter();s.call('prepare_normal',[p,codes],pr);timings['archive_prepare_inside']=time.perf_counter()-start
        start=time.perf_counter();s.call('confirm_normal',None,pr);timings['archive_commit_inside']=time.perf_counter()-start
    measure('archive_total',sign)
    # One audit change in the first store is a synthetic preview workload, not a rule change.
    p['results'][codes[0]]['qa_preview_note']='合成预览差异；数量保持不变'
    measure('preview_changed',lambda s,pr:s.call('delivery_prepare',[p,codes],pr))
    p['results'][codes[0]].pop('qa_preview_note')
    export=out/'export';export.mkdir()
    def deliver(s,pr):s.call('delivery_prepare',[p,codes],pr);s.call('delivery_execute',str(export),pr)
    measure('export_signed_total',deliver)
    assert source_hashes=={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in (source/'src').glob('*') if f.is_file()},'source changed during measurement'
    r={'passed':True,'source':str(source),'source_hashes':source_hashes,'timings':timings,'input_digest':hashlib.sha256(wr.dumps(p).encode()).hexdigest(),'rows':sum(len(r['rows']) for r in p['results'].values()),'amounts':{s:p['results'][s]['summary']['amount'] for s in codes},'files':[f.name for f in export.glob('*.xlsx')]}
    (out/'progress.json').write_text(json.dumps(messages,ensure_ascii=False),encoding='utf-8');(out/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8')

def main():
    a=argparse.ArgumentParser();a.add_argument('--child',action='store_true');a.add_argument('--source',type=Path,default=ROOT);a.add_argument('--baseline',type=Path);a.add_argument('--snapshot',type=Path,required=True);a.add_argument('--output',type=Path,required=True);a.add_argument('--runs',type=int,default=3);p=a.parse_args();out=p.output.resolve()
    if p.child:return child(p.source.resolve(),p.snapshot.resolve(),out)
    sys.path.insert(0,str(ROOT/'qa/export_071/qa-deps'));import psutil,platform,importlib.metadata
    out.mkdir(parents=True,exist_ok=False);results={'old':[],'new':[]};env=os.environ.copy();env['PYTHONHASHSEED']='0';env['PYTHONIOENCODING']='utf-8'
    for i in range(p.runs):
        for kind in (['old','new'] if i%2==0 else ['new','old']):
            dest=out/f'{kind}-{i}';dest.mkdir();source=p.baseline if kind=='old' else ROOT
            with (dest/'run.log').open('w',encoding='utf-8') as log:
                proc=subprocess.Popen([sys.executable,__file__,'--child','--source',str(source.resolve()),'--snapshot',str(p.snapshot.resolve()),'--output',str(dest)],env=env,stdout=log,stderr=log);host=psutil.Process(proc.pid);peaks={};children=set();start=time.monotonic()
                while proc.poll() is None:
                    if time.monotonic()-start>900:raise TimeoutError('benchmark PID '+str(proc.pid))
                    try:
                        phase=(dest/'phase.txt').read_text() if (dest/'phase.txt').exists() else 'startup';tree=host.children(recursive=True);children.update((q.pid,q.create_time()) for q in tree);peaks[phase]=max(peaks.get(phase,0),sum(q.memory_info().rss for q in [host]+tree))
                    except (OSError,psutil.Error):pass
                    time.sleep(.05)
            assert proc.returncode==0,(dest,proc.returncode)
            for pid,created in children:
                try:assert psutil.Process(pid).create_time()!=created,'orphan worker'
                except psutil.NoSuchProcess:pass
            r=json.loads((dest/'result.json').read_text(encoding='utf-8'));r['peak_tree_mib']={k:v/2**20 for k,v in peaks.items()};results[kind].append(r);(dest/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8');print(kind,i,r['timings'],flush=True)
    ref=results['old'][0]
    for runs in results.values():
        for r in runs:
            for key in ['input_digest','rows','amounts','files']:assert r[key]==ref[key],key
    summary={name:{k:{'seconds':[r['timings'][name] for r in runs],'median':statistics.median(r['timings'][name] for r in runs),'min':min(r['timings'][name] for r in runs),'max':max(r['timings'][name] for r in runs),'peak_mib':[r['peak_tree_mib'].get(name) for r in runs]} for k,runs in results.items()} for name in ref['timings']}
    report={'passed':True,'runs':p.runs,'summary':summary,'rows':ref['rows'],'amounts':ref['amounts'],'snapshot_sha256':hashlib.sha256(p.snapshot.read_bytes()).hexdigest(),'method':'same frozen current-model five-store job; sequential alternating old/new and new/old; no cache flush; 50ms sampled sum of parent+all child RSS (shared pages counted per process); total stages include spawn, IPC, ordering and worker teardown; inside phases exclude session launch; UI response measured separately in actual EXE','environment':{'python':sys.version,'platform':platform.platform(),'cpus':psutil.cpu_count(),'memory_mib':psutil.virtual_memory().total/2**20,'packages':{x:importlib.metadata.version(x) for x in ['openpyxl','PySide6-Essentials','PyInstaller']}}};(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8',newline='\n')
if __name__=='__main__':
    import multiprocessing
    multiprocessing.freeze_support();main()
