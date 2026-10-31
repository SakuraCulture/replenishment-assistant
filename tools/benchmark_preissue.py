"""Alternating frozen-input timings; own DB/output; same interpreter and imports."""
import argparse,json,os,subprocess,sys,time,statistics,threading
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def save(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
def worker(source,out,fixture,adjust):
    sys.path[:0]=[str(source/'src'),str(ROOT/'tools'),str(ROOT/'qa/export_071/qa-deps')]
    from parallel_qa import read
    import work_repository as wr,normal_orders as no,normal_delivery as delivery,new_product_focus,result_semantics as sem
    from version_info import MODEL_VERSION
    import psutil
    out.mkdir(parents=True);r=read(fixture);work=wr.Repository(out/'settings');repo=no.Repository(work);process=psutil.Process();stop=threading.Event();peak=[0]
    def sample():
        while not stop.is_set():
            try:peak[0]=max(peak[0],sum(x.memory_info().rss for x in [process,*process.children(recursive=True)]))
            except psutil.Error:pass
            stop.wait(.02)
    sampler=threading.Thread(target=sample);sampler.start();times={};start=time.perf_counter()
    if adjust:
        import preissue as p
        chosen=[d for d in r['rows'] if not p.blocked(d) and sem.order_quantity(d)>0][:10]
        edits={d['E']:{'quantity':0,'kind':'manual','reason':'QA合成减量','edited_at':'2026-10-31T00:00:00+08:00'} for d in chosen}
        t=time.perf_counter();v=p.preview(r,edits,wr.today());r=p.finalize(v,[x['id'] for x in v['risks']]);times['preview_seconds']=time.perf_counter()-t
    payload={'schema':1,'day':wr.today(),'model_version':MODEL_VERSION,'input_dirty':False,'results':{r['store']:r},'states':{r['store']:{'state':'已完成','name':r['store_name']}},'new_focus':new_product_focus.empty(wr.today()[:7])}
    t=time.perf_counter();saved=work.save_job(wr.today(),payload);times['save_seconds']=time.perf_counter()-t
    t=time.perf_counter();plan=delivery.prepare_batch(repo,payload,[r['store']]);times['prepare_seconds']=time.perf_counter()-t
    t=time.perf_counter();orders=repo.confirm(payload,[r['store']],saved['revision'],{r['store']:None});times['archive_seconds']=time.perf_counter()-t
    import normal_exports,warehouse_orders
    t=time.perf_counter();files=[]
    for wh in warehouse_orders.partitions(r,payload['new_focus']):
        path=out/(wh+'.xlsx');normal_exports.export(orders[0],path,warehouse=wh);files.append(str(path))
    times['export_seconds']=time.perf_counter()-t;times['total_seconds']=time.perf_counter()-start;stop.set();sampler.join();times['peak_tree_mib']=peak[0]/2**20
    save(out/'timing.json',{'timing':times,'rows':len(r['rows']),'summary':r['summary'],'files':files,'sample_ms':20,'adjusted_products':10 if adjust else 0})

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--qa',type=Path,required=True);ap.add_argument('--output',type=Path,required=True);ap.add_argument('--worker',choices=['old','new','adjusted']);a=ap.parse_args();qa=a.qa.resolve();out=a.output.resolve()
    if a.worker:return worker(qa/'baseline' if a.worker=='old' else ROOT,out,qa/'model_old/real_None.pklz',a.worker=='adjusted')
    out.mkdir(parents=True,exist_ok=False);runs={k:[] for k in ('old','new','adjusted')};env=os.environ.copy();env.update(PYTHONHASHSEED='0',PYTHONIOENCODING='utf-8')
    for i in range(3):
        for mode in (['old','new','adjusted'] if i%2==0 else ['adjusted','new','old']):
            target=out/f'{mode}-{i}'
            subprocess.run([sys.executable,__file__,'--qa',str(qa),'--output',str(target),'--worker',mode],check=True,env=env)
            runs[mode].append(json.loads((target/'timing.json').read_text(encoding='utf-8')))
    summary={mode:{k:{'median':statistics.median(x['timing'][k] for x in rr),'min':min(x['timing'][k] for x in rr),'max':max(x['timing'][k] for x in rr)} for k in rr[0]['timing']} for mode,rr in runs.items()}
    save(out/'report.json',{'runs':runs,'summary':summary,'input':'Frozen real 14244-row result; adjusted scenario synthetically edits first ten eligible positive quantities to zero, with recorded risk approval.','method':'Same interpreter, old/new alternating order, serial 3 runs each, private empty DB each run; full save/preparation/archive/export measured. OS caches uncontrolled; tree working-set sum sampled every20ms, shared pages may double count. New UI responsiveness measured separately in EXE report.','baseline':json.loads((qa/'baseline.json').read_text(encoding='utf-8'))['commit']})
    print(json.dumps(summary))
if __name__=='__main__':main()
