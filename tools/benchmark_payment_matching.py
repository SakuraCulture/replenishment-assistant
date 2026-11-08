"""Synthetic cold/warm/expanded/cancel measurements, isolated copies only."""
import argparse,json,os,subprocess,sys,time,threading,shutil,hashlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tools'),str(ROOT/'qa/export_071/qa-deps')]

def worker(out,mode):
    (out/(mode+'.pid')).write_text(str(os.getpid()),'ascii')
    import psutil,order_lifecycle as lc,payment_matching as pm,lifecycle_batch as batch
    from runtime_support import Cancelled
    path=out/'settings/workbench.sqlite3';process=psutil.Process();initial=process.io_counters();start=time.perf_counter();cancel=threading.Event();seen=0
    def progress(_):
        nonlocal seen
        seen+=1
        if mode=='cancel' and seen==3:cancel.set()
    try:
        if mode=='baseline':heads=lc.current_targets(path);result={'heads':len(heads),'head_identity':sorted((h['oid'],h['revision'],h['warehouse'],h['number']) for h in heads)}
        else:
            index=pm.build(path,progress,cancel);result={'heads':len(index['heads']),'decoded_snapshots':index['decoded_snapshots'],'head_identity':sorted((h['oid'],h['revision'],h['warehouse'],h['number']) for h in index['heads'])}
            if mode=='expanded':
                doc=batch.read([out/'付款合成.xlsx'],'payment')['documents'][0];r=pm.analyze(path,doc,index,source_scope='100');result['automatic']=r['automatic'];result['matching']=r.get('exact_numbers');result['trace']=r.get('trace')
    except Cancelled:result={'cancelled':True,'progress_steps':seen}
    final=process.io_counters();memory=process.memory_info();result.update(seconds=time.perf_counter()-start,process_read_mib=(final.read_bytes-initial.read_bytes)/1048576,process_write_mib=(final.write_bytes-initial.write_bytes)/1048576,worker_pid=os.getpid(),native_peak_worker_rss_mib=memory.peak_wset/1048576)
    (out/(mode+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')

def run(out):
    import psutil
    from verify_payment_matching import fixture
    out.mkdir(parents=True,exist_ok=False);runs=[]
    for count in (5,25,100):
        seed=out/('stores-'+str(count));fixture(seed,count)
        for mode in ('baseline','cold','warm','expanded','cancel'):
            dest=seed
            if mode=='cancel':
                dest=out/('cancel-'+str(count));shutil.copytree(seed,dest,ignore=shutil.ignore_patterns('payment_matching'))
            before=hashlib.sha256((dest/'settings/workbench.sqlite3').read_bytes()).hexdigest()
            peak=0;cmd=[sys.executable,__file__,'worker','--out',str(dest),'--mode',mode]
            with (dest/(mode+'.log')).open('wb') as log:
                p=subprocess.Popen(cmd,cwd=ROOT,stdout=log,stderr=log);proc=psutil.Process(p.pid);started=time.monotonic()
                while p.poll() is None:
                    try:
                        pidfile=dest/(mode+'.pid')
                        if pidfile.exists():peak=max(peak,psutil.Process(int(pidfile.read_text('ascii'))).memory_info().rss)
                    except psutil.Error:pass
                    if time.monotonic()-started>300:raise TimeoutError(f'Isolated worker {p.pid} retained')
                    time.sleep(.02)
            if p.returncode:raise RuntimeError(str(dest/(mode+'.log')))
            result=json.loads((dest/(mode+'.json')).read_text('utf-8'));assert before==hashlib.sha256((dest/'settings/workbench.sqlite3').read_bytes()).hexdigest()
            result.update(stores=count,mode=mode,sampled_peak_worker_rss_mib=peak/1048576);runs.append(result)
        subset=runs[-5:];assert subset[0]['head_identity']==subset[1]['head_identity']==subset[2]['head_identity'];assert subset[2]['decoded_snapshots']==0 and subset[-1]['cancelled']
    for r in runs:r.pop('head_identity',None)
    report={'passed':True,'synthetic':True,'live_platform':False,'rounds':1,'runs':runs,'limitations':'Tiny synthetic SKU sets; Windows actual worker process high-water RSS and 20ms sampled RSS (launcher excluded), OS-cached process I/O, not physical disk or whole-app memory. Baseline reads old full current-target path; cold index additionally computes generation summaries. Expanded includes file read and exact comparisons. No low-end business PC measurement.'}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');print(json.dumps(report,ensure_ascii=False))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['run','worker']);p.add_argument('--out',type=Path,required=True);p.add_argument('--mode');a=p.parse_args();run(a.out.resolve()) if a.action=='run' else worker(a.out.resolve(),a.mode)
