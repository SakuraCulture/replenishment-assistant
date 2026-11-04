"""Alternating real-input service/gateway measurement in an isolated Qt workspace."""
import argparse,copy,json,os,sys,threading,time,statistics,queue,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tools')]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--single',choices=['service','gateway']);parser.add_argument('--repeat',type=int,default=1);a=parser.parse_args()
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False)
    if not a.single:
        from parallel_qa import read,clean,diff
        records=[];latencies=[];oracle=None
        for repeat in range(1,4):
            for mode in (['service','gateway'] if repeat%2 else ['gateway','service']):
                folder=out/f'{repeat}-{mode}'
                subprocess.run([sys.executable,__file__,'--out',str(folder),'--single',mode,'--repeat',str(repeat)],check=True)
                child=json.loads((folder/'performance.json').read_text('utf-8'));record=child['records'][0]
                actual=read(folder/'result.pklz')
                if oracle is None:oracle=actual
                differences=diff(clean(oracle),clean(actual))
                if differences:raise AssertionError(differences[:10])
                record['difference_count']=len(differences);record['isolated_startup_seconds']=child['isolated_startup_seconds'];records.append(record);latencies.extend(child['status_latency_seconds'])
        summary={m:{k:{'median':statistics.median(v),'min':min(v),'max':max(v)} for k in ('total_seconds','prepare_seconds','calculate_seconds','peak_tree_rss_bytes','isolated_startup_seconds') for v in [[r[k] for r in records if r['mode']==m]]} for m in ('service','gateway')}
        report={'records':records,'summary':summary,'status_latency_seconds':latencies,'status_p95':sorted(latencies)[int(len(latencies)*.95)-1],
            'passed':True,'rows':child['rows'],'stores':child['stores'],'source':child['source'],
            'method':child['method']+' Each measurement uses a fresh process/workspace; previous trial caches cannot affect next peak. Same frozen checked input preloaded from QA pickle outside measured check/calculate interval. Initial Qt/new empty workspace startup reported separately, not production history cold-index performance.'}
        (out/'performance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');return
    os.environ['REPLENISHMENT_DATA_DIR']=str(out/'settings')
    os.environ['QT_QPA_PLATFORM']='offscreen'
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from workbench import Workbench
    from automation_gateway import Gateway,Job
    from automation_bridge import Client
    import batch_service,automation_jobs
    from windows_process_metrics import sample_tree
    from parallel_qa import clean,diff,read,dump
    startup=time.perf_counter();app=QApplication([]);win=Workbench();startup=time.perf_counter()-startup
    inp=ROOT/'qa/v0350/inputs';sales=inp/'补货建议导出服务.xlsx';master=inp/'混合仓商品资料.xlsx'
    win.master_legacy.setChecked(True);win.products.path=str(master);win.sales.paths=[str(sales)]
    win.loaded_ok(read(ROOT/'qa/v0350/before/real_loaded.pklz'))
    codes=list(win.loaded['stores']);win.concurrency.setValue(2)
    for s in codes:win.store_widgets[s]['mode'].setCurrentIndex(1)
    win.input_dirty=False;g=Gateway(win);g.enable()
    bridge_log=(out/'bridge.log').open('w',encoding='utf-8')
    client=subprocess.Popen([sys.executable,str(ROOT/'src/automation_bridge.py'),'--connection',str(g.connection_file)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=bridge_log,text=True,encoding='utf-8',creationflags=subprocess.CREATE_NO_WINDOW)
    replies=queue.Queue();sequence=[0]
    def reader():
        for line in client.stdout:replies.put(json.loads(line))
    threading.Thread(target=reader,daemon=True).start()
    def wait(pred):
        end=time.monotonic()+180
        while not pred() and time.monotonic()<end:app.processEvents();QTest.qWait(2)
        if not pred():raise TimeoutError('Work did not finish')
    def rpc(method,params):
        sequence[0]+=1;client.stdin.write(json.dumps({'jsonrpc':'2.0','id':sequence[0],'method':method,'params':params})+'\n');client.stdin.flush()
        wait(lambda:not replies.empty());v=replies.get()
        if 'error' in v:raise RuntimeError(v)
        return v['result']
    def call(name,args=None):
        v=rpc('tools/call',{'name':name,'arguments':args or {}})
        if v.get('isError'):raise RuntimeError(v)
        return v['structuredContent']
    rpc('initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'performance','version':'1'}})
    records=[];oracle=None;latencies=[];stop=threading.Event();peak=[0]
    def sample():
        while not stop.wait(.02):
            total=sum(p['rss'] for p in sample_tree(os.getpid()).values())
            peak[0]=max(peak[0],total)
    sampler=threading.Thread(target=sample);sampler.start()
    try:
        for repeat in [a.repeat-1]:
            for mode in [a.single]:
                peak[0]=0;start=time.perf_counter();phases={};actual={}
                if mode=='service':
                    snapshot=g.snapshot({});ready=[];errors=[]
                    def fn(w):
                        nonlocal phases
                        prepared=automation_jobs.prepare(snapshot,{'paths':[str(sales)],'stores':codes,'mode':'trial'},w.progress.emit,w.cancel)
                        phases['prepared']=time.perf_counter()-start
                        # This preparation shares exactly the same input/config adapter; no IPC or task journal.
                        batch_service.run(prepared['loaded'],prepared['jobs'],prepared['profile'],2,
                            lambda s,state,value,error: actual.update({s:value}) if value else None,w.cancel)
                    worker=Job(fn,win);worker.error_ready.connect(lambda c,m:errors.append(m));worker.finished.connect(lambda:ready.append(True));worker.start()
                    wait(lambda:bool(ready));worker.wait();worker.deleteLater()
                    if errors:raise AssertionError(errors)
                else:
                    p=call('prepare_calculation',{'context_token':g.token(),'operation_key':f'perf-{repeat}','mode':'trial','paths':[str(sales)],'stores':codes})
                    phases['accepted']=time.perf_counter()-start
                    wait(lambda:g.worker is None);phases['prepared']=time.perf_counter()-start
                    if call('task_status',{'task_id':p['task_id']})['state']!='prepared':raise AssertionError('Input failed')
                    task=call('run_calculation',{'preview_id':p['task_id']});wait(lambda:g.worker is None)
                    actual=g.require_result(task['task_id'])['results']
                    for _ in range(20):
                        t=time.perf_counter();call('task_status',{'task_id':task['task_id']});latencies.append(time.perf_counter()-t)
                elapsed=time.perf_counter()-start
                if mode=='gateway':elapsed-=sum(latencies[-20:])
                if oracle is None:oracle=copy.deepcopy(actual)
                differences=diff(clean(oracle),clean(actual))
                if differences:raise AssertionError(differences[:10])
                records.append({'mode':mode,'repeat':repeat+1,'total_seconds':elapsed,'prepare_seconds':phases['prepared'],
                    'calculate_seconds':elapsed-phases['prepared'],'accept_seconds':phases.get('accepted'),
                    'peak_tree_rss_bytes':peak[0],'difference_count':len(differences)})
                print(json.dumps(records[-1]),flush=True)
        dump(out/'result.pklz',actual)
        report={'records':records,'status_latency_seconds':latencies,'isolated_startup_seconds':startup,
            'rows':len(win.loaded['normalized']),'stores':codes,'passed':True,'source':'Frozen real 14244-row input and original master, unlimited budget; same Python/dependencies',
            'method':'Alternating serial runs, both original shared preparation/calculation; gateway additionally named-pipe JSON roundtrip/task journal. Warm Windows file cache, no cache flushing. Peak sum RSS of this process and descendants sampled every 20ms; includes shared pages per process. Latency probes excluded from total; cold app startup excluded.'}
        (out/'performance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    finally:
        stop.set();sampler.join()
        if g.worker:g.worker.cancel.set();wait(lambda:g.worker is None)
        client.stdin.close();client.wait(10);bridge_log.close()
        g.disable();win._discard_close=True;win.job_dirty=False;win.close();app.processEvents()


if __name__=='__main__':
    import multiprocessing
    multiprocessing.freeze_support();main()
