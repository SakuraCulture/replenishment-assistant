"""Alternating serial A/B, complete operations including spawn; 50ms RSS tree samples."""
import argparse,json,os,pickle,statistics,subprocess,sys,time,zlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];Q=ROOT/'qa/verified_092'

def child(source,mode,out):
    sys.path.insert(0,str(source/'src'))
    import engine,batch_service,file_intake
    loaded=pickle.loads(zlib.decompress((Q/'regression_old/real_loaded.pklz').read_bytes()))
    profile=json.loads((ROOT/'qa/parallel_062/inputs/must.json').read_text(encoding='utf-8'))
    params=json.loads((ROOT/'qa/parallel_062/inputs/parameters.json').read_text(encoding='utf-8'))['standard']
    store=next(iter(loaded['stores']));settings={'warehouses':engine.DEFAULT_WAREHOUSES,'exception':True,'budget':3000,'parameter_config':params}
    started=time.perf_counter()
    if mode=='read':
        r=file_intake.check_file(0,str(ROOT/'qa/parallel_062/inputs/补货建议导出服务.xlsx'));summary=r['report']
    elif mode=='calculate':r=batch_service.calculate_store(loaded,store,settings,profile);summary=r['summary']
    else:
        r=batch_service.run(loaded,{store:settings},profile,max_workers=2);summary=r[store]['summary']
    out.write_text(json.dumps({'seconds':time.perf_counter()-started,'summary':summary},ensure_ascii=False),encoding='utf-8')

def main():
    p=argparse.ArgumentParser();p.add_argument('--child',action='store_true');p.add_argument('--source',type=Path);p.add_argument('--mode');p.add_argument('--out',type=Path);a=p.parse_args()
    if a.child:return child(a.source,a.mode,a.out)
    sys.path.insert(0,str(ROOT/'qa/export_071/qa-deps'));import psutil
    out=Q/'performance';out.mkdir(exist_ok=False);runs=[]
    for mode in ('read','calculate','batch'):
        for repeat in range(3):
            for variant in (('old','new') if repeat%2==0 else ('new','old')):
                source=Q/'baseline' if variant=='old' else ROOT;target=out/f'{mode}_{repeat}_{variant}.json'
                env=os.environ.copy();env['PYTHONHASHSEED']='0';env['REPLENISHMENT_DATA_DIR']=str(out/'private_settings')
                proc=subprocess.Popen([sys.executable,__file__,'--child','--source',str(source),'--mode',mode,'--out',str(target)],env=env);host=psutil.Process(proc.pid);peak=0
                while proc.poll() is None:
                    try:peak=max(peak,sum(p.memory_info().rss for p in [host]+host.children(recursive=True)))
                    except psutil.Error:pass
                    time.sleep(.05)
                if proc.returncode:raise RuntimeError('measurement failed '+str(target))
                r=json.loads(target.read_text(encoding='utf-8'));r.update(mode=mode,repeat=repeat,variant=variant,peak_tree_mib=peak/2**20);runs.append(r);print(mode,repeat,variant,round(r['seconds'],3),round(r['peak_tree_mib'],1),flush=True)
    summary=[]
    for mode in ('read','calculate','batch'):
        for variant in ('old','new'):
            rs=[r for r in runs if r['mode']==mode and r['variant']==variant];times=[r['seconds'] for r in rs];mem=[r['peak_tree_mib'] for r in rs]
            summary.append({'mode':mode,'variant':variant,'median_seconds':statistics.median(times),'min_seconds':min(times),'max_seconds':max(times),'median_peak_mib':statistics.median(mem),'peak_mib':max(mem)})
    for mode in ('read','calculate','batch'):
        for variant in ('old','new'):
            values=[r['summary'] for r in runs if r['mode']==mode and r['variant']==variant]
            if any(v!=values[0] for v in values):raise ValueError('repeated measurement results differ '+mode)
    (Q/'performance.json').write_text(json.dumps({'passed':True,'runs':runs,'summary':summary,'method':'same Python/dependencies; alternating sequential processes, 3 runs; read includes hashing/open/read/check; calculate includes routing/parameters/model/budget; batch includes copying/pickling/spawn; RSS sum parent+children every50ms includes child interpreter/loading; OS caches not flushed; 14244 actual rows, one-store batch; final new budget rule explicitly increases one candidate as verified independently in overrun_equivalence.json'},ensure_ascii=False,indent=2),encoding='utf-8')
if __name__=='__main__':
    import multiprocessing
    multiprocessing.freeze_support();main()
