"""Same frozen inputs, alternating separate processes, three trials; generated data."""
import argparse,copy,hashlib,json,shutil,sqlite3,statistics,subprocess,sys,threading,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def save(p,v):Path(p).write_bytes(json.dumps(v,ensure_ascii=False,indent=2).encode())
def worker(a):
    sys.path[:0]=[str(a.source/'src'),str(ROOT/'qa/export_071/qa-deps')]
    import lifecycle_io as io,order_lifecycle as lc,purchases,lifecycle_home,psutil
    a.output.mkdir(parents=True,exist_ok=False);db=a.output/'workbench.sqlite3';shutil.copy2(a.fixture/'settings/workbench.sqlite3',db);times={};peak=[0];stop=threading.Event();p=psutil.Process()
    def sample():
        while not stop.wait(.02):peak[0]=max(peak[0],p.memory_info().rss)
    t=threading.Thread(target=sample);t.start();start=time.perf_counter()
    def timed(k,fn):
        before=time.perf_counter();r=fn();times[k]=time.perf_counter()-before;return r
    try:
        heads=timed('target',lambda:purchases.targets(db));h=next(h for h in heads if h['warehouse']=='河北仓')
        for stage in io.STAGES:
            def scan():
                f=io.inspect([a.fixture/(stage+'.xlsx')])[0];s=f['sheets'][0];return io.scan(stage,[{**f,**s,'mapping':{k:s['headers'].index(k) if k in s['headers'] else None for k in io.fields(stage)}}])
            data=timed(stage+'_read',scan);bindings={lc.binding_key(r):h for r in data['rows']};plan=timed(stage+'_preview',lambda:lc.prepare(db,data,bindings));timed(stage+'_save',lambda:lc.commit(db,plan))
        for kind in ('cold','warm'):timed('home_'+kind,lambda:lifecycle_home.read(db,a.output/'index.db',{}))
        r=timed('comparison_export',lambda:lc.export(db,[h],a.output,'comparison'))
        if r['error'] or len(r['files'])!=1:raise ValueError(r)
        view=lc.snapshot(db,h);quantities=[[r[0],*r[2:9]] for r in view['rows']]
        if len(quantities)!=a.rows or any(r[2:5]!=[6,4,3] for r in quantities):raise ValueError('measured quantities differ')
        times['total']=time.perf_counter()-start
    finally:stop.set();t.join()
    save(a.output/'result.json',{'seconds':times,'peak_rss_mib':peak[0]/2**20,'quantities_sha':hashlib.sha256(json.dumps(quantities).encode()).hexdigest(),'source_hashes':{n:hashlib.sha256((a.source/'src'/n).read_bytes()).hexdigest() for n in ('lifecycle_io.py','order_lifecycle.py','lifecycle_home.py')}})

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--baseline',type=Path);ap.add_argument('--output',type=Path,required=True);ap.add_argument('--source',type=Path);ap.add_argument('--fixture',type=Path);ap.add_argument('--rows',type=int,default=5000);a=ap.parse_args()
    if a.source:return worker(a)
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);fixture=out/'fixture';fixture.mkdir();sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
    from test_archive_orders import seed
    import normal_orders as no,work_repository as wr,openpyxl,lifecycle_io as io
    work,repo,p=seed(fixture);template=p['result']['rows'][0];p['result']['rows']=[]
    for i in range(a.rows):
        r=copy.deepcopy(template);r['E']=f'{i:06d}';r['D']='合成商品'+str(i);r['_execution_qty']=12;r['_execution_amount']=12*r['V'];p['result']['rows'].append(r)
    with sqlite3.connect(work.path) as db:db.execute('UPDATE normal_revisions SET digest=?,payload=?',(no.fingerprint(p['result']),no.encode(p)))
    for stage,qty in [('return',6),('payment',4),('receipt',3)]:
        b=openpyxl.Workbook(write_only=True);s=b.create_sheet('明细');headers=list(io.fields(stage))
        if stage=='payment':headers.insert(headers.index('付款单号')+1,'付款日期')
        s.append(headers)
        for i in range(a.rows):
            row={'补货单号':'BH-T-ZC-HB','签发修订':1,'门店编码':'T','供货仓':'河北仓','商品编码':f'{i:06d}','商品名称':'合成商品'+str(i),'数量单位':'库存单位',io.QUANTITIES[stage]:qty,io.DOCNUM[stage]:'D001',io.DOCDATE[stage]:wr.today(),'实际金额':5.2,'币种':'CNY'};s.append([row.get(k) for k in headers])
        b.save(fixture/(stage+'.xlsx'));b.close()
    hashes={str(p.relative_to(fixture)):hashlib.sha256(p.read_bytes()).hexdigest() for p in fixture.rglob('*') if p.is_file()};runs=[]
    for trial in range(3):
        for name,src in ([('old',a.baseline),('new',ROOT)] if trial%2==0 else [('new',ROOT),('old',a.baseline)]):
            dest=out/f'{trial+1}-{name}';subprocess.run([sys.executable,__file__,'--source',str(src.resolve()),'--fixture',str(fixture),'--rows',str(a.rows),'--output',str(dest)],check=True)
            runs.append({'trial':trial+1,'variant':name,**json.loads((dest/'result.json').read_text())})
    def stats(xs):return {'median':statistics.median(xs),'min':min(xs),'max':max(xs)}
    summary={v:{k:stats([r['seconds'][k] for r in runs if r['variant']==v]) for k in runs[0]['seconds']} for v in ('old','new')}
    memory={v:stats([r['peak_rss_mib'] for r in runs if r['variant']==v]) for v in ('old','new')}
    after={str(p.relative_to(fixture)):hashlib.sha256(p.read_bytes()).hexdigest() for p in fixture.rglob('*') if p.is_file()}
    passed=after==hashes and len({r['quantities_sha'] for r in runs})==1
    report={'passed':passed,'synthetic':True,'rows_per_stage':a.rows,'runs':runs,'summary':summary,'peak_memory_mib':memory,'input_hashes':hashes,'inputs_unchanged':after==hashes,'method':'Same PC/Python/dependencies; old/new alternate sequentially, 3 each, separate worker processes. Time includes reading, preview, save incl first safety backup, home cold/warm and comparison export. RSS sampled every 20ms in service process, no GUI; filesystem cache not flushed. New reconciliation does extra predecessor/audit work; this is not a promise of speedup. Common populated inputs ensure comparable numeric results; authorized blank/missing changes are separate functional tests.'};save(out/'report.json',report)
    if not passed:raise ValueError('Measured outputs/input hashes differ')
    print(json.dumps({'passed':passed,'total':{k:v['total'] for k,v in summary.items()},'memory':memory}))
if __name__=='__main__':main()
