"""Alternating immutable-source comparison, isolated data and exact payment facts."""
import argparse,hashlib,json,os,shutil,statistics,subprocess,sys,time,threading
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def write(p,value):p.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n','utf-8',newline='\n')
def worker(a):
    sys.path[:0]=[str(a.source/'src'),str(a.deps)]
    import psutil,payment_matching as pm,lifecycle_batch as batch,order_lifecycle as lc,work_repository as wr
    from unittest.mock import patch
    shutil.copytree(a.fixture/'settings',a.out/'settings',ignore=shutil.ignore_patterns('payment_matching'))
    path=a.out/'settings/workbench.sqlite3';process=psutil.Process();report=[]
    source_hashes={p.name:sha(p) for p in (a.source/'src').glob('*.py')}
    docs={name:batch.read([a.fixture/(name+'.xlsx')],'payment')['documents'][0] for name in ('完整付款','差异付款')}
    def measure(name,call):
        peak=[process.memory_info().rss];stop=threading.Event()
        def sample():
            while not stop.wait(.01):peak[0]=max(peak[0],process.memory_info().rss)
        t=threading.Thread(target=sample);t.start();count=[0];exact=pm.exact;start=time.perf_counter()
        def counted(*args,**kwargs):count[0]+=1;return exact(*args,**kwargs)
        try:
            with patch('payment_matching.exact',counted):value=call()
            elapsed=time.perf_counter()-start
        finally:stop.set();t.join()
        peak[0]=max(peak[0],process.memory_info().rss)
        report.append({'phase':name,'seconds':elapsed,'peak_worker_rss_mib':peak[0]/2**20,'exact_candidate_reads':count[0]});return value
    original=sha(path);index=measure('cold_index',lambda:pm.build(path));cold=index['decoded_snapshots']
    index=measure('warm_index',lambda:pm.build(path));assert index['decoded_snapshots']==0
    recognized=measure('name_recognition',lambda:pm.analyze(path,docs['完整付款'],index))
    head=next(h for h in index['heads'] if h['number']==json.loads((a.fixture/'bench.json').read_text('utf-8'))['number'])
    docs['差异付款']['target']=head
    plan=measure('difference_prepare',lambda:pm.prepare(path,[docs['差异付款']],None,index,None));assert sha(path)==original
    business=pm.checksum([{k:v for k,v in p.items() if k!='_read_proofs'} for p in plan['plans']])
    receipt=measure('difference_commit',lambda:lc.commit(path,plan));view=lc.snapshot(path,head)
    result={'phases':report,'recognition_automatic':recognized['automatic'],'recognition_candidates':len(recognized['candidates']),
            'cold_decoded':cold,'payment_plan_sha256':business,'payment_payload_digest':view['documents']['payment']['digest'],
            'payment_rows_sha256':pm.checksum(view['documents']['payment']['payload']['rows']),'receipt':receipt,'source_sha256':source_hashes,'variant':a.variant,'trial':a.trial}
    assert all(sha(a.source/'src'/p)==h for p,h in source_hashes.items());write(a.out/'report.json',result)
def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--source',type=Path,required=True);p.add_argument('--baseline',type=Path);p.add_argument('--fixture',type=Path);p.add_argument('--deps',type=Path,required=True);p.add_argument('--worker',action='store_true');p.add_argument('--variant');p.add_argument('--trial',type=int);a=p.parse_args()
    if a.worker:return worker(a)
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False)
    sys.path[:0]=[str(a.source/'src'),str(ROOT/'tools'),str(ROOT/'tests')]
    from verify_payment_matching import fixture
    import payment_shop_names as names,jst_orders as jst,openpyxl
    seed=out/'fixture';path=fixture(seed,40);info=json.loads((seed/'matching-fixture.json').read_text('utf-8'));head=info['head'];g=jst.get(path,info['gids'][0])
    names.save(path,names.prepare(names.load(path),[{'store':head['store'],'warehouse':head['warehouse'],'value':'性能对照专用仓店名'}]))
    for name,difference in [('完整付款',False),('差异付款',True)]:
        w=openpyxl.Workbook();s=w.active;s.append(['内部订单号','店铺名称','商品编码','数量','商品金额'])
        for r in g['rows']:
            if float(r['quantity'])>0:s.append(['BENCH-ONE','性能对照专用仓店名',r['sku'],'7' if difference else r['quantity'],'8.25' if difference else r['amount']])
        w.save(seed/(name+'.xlsx'));w.close()
    write(seed/'bench.json',{'number':head['number']});before=sha(path);runs=[]
    for trial in (1,2,3):
        for variant in (('old','new') if trial%2 else ('new','old')):
            source=a.baseline.resolve() if variant=='old' else a.source.resolve();dest=out/f'{trial}-{variant}';dest.mkdir()
            cmd=[sys.executable,str(Path(__file__).resolve()),'--worker','--out',str(dest),'--fixture',str(seed),'--source',str(source),'--deps',str(a.deps.resolve()),'--variant',variant,'--trial',str(trial)]
            with (dest/'process.log').open('wb') as log:process=subprocess.run(cmd,stdout=log,stderr=log,env={**os.environ,'PYTHONUTF8':'1'})
            if process.returncode:raise RuntimeError('Worker failed, retained '+str(dest))
            runs.append(json.loads((dest/'report.json').read_text('utf-8')));print(f'completed {trial} {variant}',flush=True)
    equivalence={field:len({json.dumps(r[field],ensure_ascii=False,sort_keys=True) for r in runs})==1 for field in ('payment_plan_sha256','payment_payload_digest','payment_rows_sha256','receipt')}
    assert all(equivalence.values()) and sha(path)==before
    medians={variant:{phase:{metric:statistics.median(next(v for v in r['phases'] if v['phase']==phase)[metric] for r in runs if r['variant']==variant) for metric in ('seconds','peak_worker_rss_mib','exact_candidate_reads')} for phase in ('cold_index','warm_index','name_recognition','difference_prepare','difference_commit')} for variant in ('old','new')}
    result={'passed':True,'synthetic':True,'live_platform':False,'stores':40,'orders':80,'rounds':3,'business_equivalence':equivalence,'medians':medians,'runs':runs,
            'limitations':'Each synthetic warehouse order has one SKU. Recognition intentionally changes with configured names; old ambiguous candidates versus new unique identity. Difference preparation uses explicit manual target in old version. Ten-ms sampled worker RSS excludes launcher/whole app and is not instantaneous peak. Backend only, not low-end business PC or live platform. All source roots immutable and checked.'}
    write(out/'report.json',result);print(json.dumps({'passed':True,'business_equivalence':equivalence,'medians':medians},ensure_ascii=False))
if __name__=='__main__':main()
