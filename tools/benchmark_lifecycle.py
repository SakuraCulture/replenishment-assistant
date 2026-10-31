"""Sequential alternating home comparison + three measured downstream service runs."""
import argparse,hashlib,json,os,shutil,statistics,subprocess,sys,threading,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def worker(args):
    sys.path[:0]=[str(args.source/'src'),str(ROOT/'qa/export_071/qa-deps')]
    import psutil
    if args.variant=='old':from followup import read
    else:from lifecycle_home import read
    args.output.mkdir(parents=True,exist_ok=False);db=args.output/'workbench.sqlite3';shutil.copy2(args.fixture,db)
    peak=[0];stop=threading.Event();p=psutil.Process()
    def sample():
        while not stop.wait(.02):peak[0]=max(peak[0],p.memory_info().rss)
    t=threading.Thread(target=sample);t.start();times={}
    try:
        for state in ('cold','warm'):
            start=time.perf_counter();r=read(db,args.output/'index.db',{});times[state]=time.perf_counter()-start
            if r['scope']!=1000:raise AssertionError('fixture scope differs')
    finally:stop.set();t.join()
    (args.output/'result.json').write_text(json.dumps({'seconds':times,'peak_rss_mib':peak[0]/2**20}),encoding='utf-8')

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True);ap.add_argument('--baseline',type=Path);ap.add_argument('--fixture',type=Path,required=True);ap.add_argument('--worker',action='store_true');ap.add_argument('--source',type=Path);ap.add_argument('--variant');a=ap.parse_args()
    if a.worker:return worker(a)
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);measurements=[]
    for i in range(3):
        for variant in (('old','new') if i%2==0 else ('new','old')):
            folder=out/f'{i+1}-{variant}';src=a.baseline.resolve() if variant=='old' else ROOT
            subprocess.run([sys.executable,__file__,'--worker','--variant',variant,'--source',str(src),'--fixture',str(a.fixture.resolve()),'--output',str(folder)],check=True)
            measurements.append({'trial':i+1,'variant':variant,**json.loads((folder/'result.json').read_text())})
    sys.path[:0]=[str(ROOT/'src'),str(ROOT/'qa/export_071/qa-deps')]
    import openpyxl,purchases,order_lifecycle as lc,lifecycle_io as io,work_repository as wr,lifecycle_home,psutil
    source=out/'synthetic-payment.xlsx';book=openpyxl.Workbook(write_only=True);ws=book.create_sheet('门店付款单');ws.append(io.fields('payment'))
    for n in range(50):
        store=f'T{n:04d}'
        for j in range(100):
            wh='河北仓' if j%2==0 else '山东仓';suffix='HB' if j%2==0 else 'SD';r={'补货单号':'BH-'+store+'-ZC-'+suffix,'签发修订':1,'门店编码':store,'供货仓':wh,'商品编码':f'{j:05d}','商品名称':'合成商品'+str(j),'数量单位':'库存单位','付款单商品数量':5,'付款单号':'P'+store+suffix,'付款日期':wr.today(),'实际金额':12,'币种':'CNY'};ws.append([r.get(f) for f in io.fields('payment')])
    book.save(source);book.close();runs=[];process=psutil.Process()
    for i in range(3):
        folder=out/f'service-{i+1}';folder.mkdir();db=folder/'workbench.sqlite3';shutil.copy2(a.fixture,db);times={};peak=[0];stop=threading.Event()
        def sample():
            while not stop.wait(.02):peak[0]=max(peak[0],process.memory_info().rss)
        t=threading.Thread(target=sample);t.start()
        def timed(key,fn):
            start=time.perf_counter();value=fn();times[key]=time.perf_counter()-start;return value
        try:
            def read():
                info=io.inspect([source])[0];s=info['sheets'][0];return io.scan('payment',[{**info,**s,'mapping':{f:j for j,f in enumerate(io.fields('payment'))}}])
            scan=timed('inspect_and_read_5000_lines',read);heads=timed('load_100_exact_targets',lambda:lc.available(db,scan));by_number={h['number']:h for h in heads};bindings={lc.binding_key(r):by_number[r['signed_number']] for r in scan['rows']};plan=timed('prepare_100_complete_orders',lambda:lc.prepare(db,scan,bindings));receipt=timed('commit_including_first_backup',lambda:lc.commit(db,plan));home=timed('home_after_payment_cold',lambda:lifecycle_home.read(db,folder/'index.db',{}));ex=timed('export_100_orders_combined_per_warehouse',lambda:lc.export(db,heads,folder,'comparison','combined'))
            if len(receipt)!=100 or home['scope']!=1000 or home['counts']['待回传']!=1000 or len(ex['files'])!=2 or ex['error']:raise AssertionError('measured result differs')
        finally:stop.set();t.join()
        runs.append({'seconds':times,'peak_rss_mib':peak[0]/2**20})
    def stats(values):return {'median':statistics.median(values),'min':min(values),'max':max(values)}
    report={'passed':True,'synthetic':True,'fixture_sha256':hashlib.sha256(a.fixture.read_bytes()).hexdigest(),'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'scale':{'warehouse_orders':1000,'signed_rows':50000,'payment_rows':5000,'payment_documents':100},'home_runs':measurements,'home':{v:{k:stats([r['seconds'][k] for r in measurements if r['variant']==v]) for k in ('cold','warm')} for v in ('old','new')},'service_runs':runs,'service':{k:stats([r['seconds'][k] for r in runs]) for k in runs[0]['seconds']},'measurement':'Same Python/dependencies/PC; old/new home alternate sequentially in separate processes; new services 3 sequential runs. 20ms service-process RSS samples, excludes GUI/spawn startup. Filesystem cache not flushed; cold refers to empty projection cache. New paid/receipt operations have no old semantic equivalent; no speedup claimed for those.','python':sys.version}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({'home':report['home'],'service':report['service']}))
if __name__=='__main__':main()
