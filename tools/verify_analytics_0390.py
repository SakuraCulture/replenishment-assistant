"""Portable synthetic analytics benchmark and immutable-input model comparison.

Use a new --output directory. Does not read user settings or real input files.
"""
import argparse,copy,hashlib,json,os,pickle,sqlite3,subprocess,sys,time,threading,statistics,zipfile
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
import ctypes,openpyxl

def rss():
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)]+[(n,ctypes.c_size_t) for n in ('PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]
    p=Counters();p.cb=ctypes.sizeof(p);kernel=ctypes.WinDLL('kernel32',use_last_error=True);kernel.GetCurrentProcess.restype=wintypes.HANDLE
    api=ctypes.WinDLL('psapi',use_last_error=True).GetProcessMemoryInfo;api.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
    if not api(kernel.GetCurrentProcess(),ctypes.byref(p),p.cb):raise ctypes.WinError(ctypes.get_last_error())
    return p.WorkingSetSize

def timed(fn):
    stop=threading.Event();samples=[]
    def sample():
        while not stop.wait(.03):
            samples.append(rss())
    monitor=threading.Thread(target=sample);monitor.start();started=time.perf_counter()
    try:result=fn();elapsed=time.perf_counter()-started
    finally:stop.set();monitor.join()
    return result,{'seconds':elapsed,'peak_sampled_rss_mb':max(samples,default=rss())/1024**2}

def fixture(out,stores=12,skus=500):
    import test_order_analytics as fixtures
    import normal_orders as no,source_archive,work_repository as wr
    f=fixtures.AnalyticsTests();f.setUp();path=out/'settings'/'workbench.sqlite3';path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(f.path)) as src,closing(sqlite3.connect(path)) as dst:src.backup(dst)
    payload=copy.deepcopy(f.p);f.tearDown();catalog=out/'synthetic-products.xlsx';book=openpyxl.Workbook();ws=book.active;ws.title='原资料';ws.append(['商品编码','商品状态','虚拟分类'])
    for i in range(skus):ws.append([f'{i:06d}','启用','标杆淘汰' if i%31==0 else None])
    book.save(catalog);book.close();sha=hashlib.sha256(catalog.read_bytes()).hexdigest();source_archive.capture(path,catalog,sha)
    with closing(sqlite3.connect(path)) as db,db:
        db.execute('DELETE FROM normal_orders');db.execute('DELETE FROM normal_revisions')
        catalogs={w:{'source_sha256':sha,'sheet':'原资料','revision':sha,'items':{}} for w in ('山东仓','河北仓')}
        db.execute('UPDATE normal_work_revisions SET payload=?',(no.encode({'catalogs':catalogs}),))
        for s in range(stores):
            p=copy.deepcopy(payload);result=p['result'];result['store']=f'S{s:03d}';result['store_name']=f'合成门店{s:03d}'
            base=result['rows'][0];rows=[]
            for i in range(skus):
                row=copy.deepcopy(base);row.update(E=f'{i:06d}',D=f'合成商品{i:06d}',AU='河北仓' if i%2 else '山东仓',_must_sell=i%3==0)
                rows.append(row)
            result['rows']=rows;result['must_list']={'revision':'synthetic','total':len([r for r in rows if r['_must_sell']])};result['must_list_audit']=[{'sku':r['E']} for r in rows if r['_must_sell']]
            oid=f'bench-{s:03d}';day=wr.today();db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(day,result['store'],'normal_model',oid,'BH-'+oid,1));db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',(oid,1,no.fingerprint(result),wr.now(),no.encode(p)))
    return path,{'from':wr.today(),'to':wr.today()}

def benchmark(out,path,spec):
    import order_analytics as a,order_analytics_exports as e,lifecycle_comparison as old,purchases
    heads=purchases.targets(path);runs={'old_four_stage_query':[],'new_five_stage_query':[],'new_complete_export':[]}
    original=hashlib.sha256(path.read_bytes()).hexdigest()
    for i in range(3):
        for kind in (('old','new') if i%2==0 else ('new','old')):
            if kind=='old':_,r=timed(lambda:old.collect(path,heads));runs['old_four_stage_query'].append(r)
            else:report,r=timed(lambda:a.collect(path,spec));runs['new_five_stage_query'].append(r)
        _,r=timed(lambda:e.export(path,report,out/f'analytics-{i}.xlsx'));runs['new_complete_export'].append(r)
    if hashlib.sha256(path.read_bytes()).hexdigest()!=original:raise AssertionError('分析改变了数据库')
    return {'synthetic':True,'store_count':12,'sku_per_store':500,'rows':len(report['rows']),
            'limitation':'温文件缓存，同机串行，旧四段仅差异明细，新五段全量及历史核对；非同功能提速结论。GetProcessMemoryInfo每30ms采样本进程工作集，查询/导出无子进程，含解释器；可能遗漏瞬时峰值。未测计算子进程内存。',
            'runs':runs,'summary':{k:{'median_seconds':statistics.median(x['seconds'] for x in v),'min_seconds':min(x['seconds'] for x in v),'max_seconds':max(x['seconds'] for x in v),'max_rss_mb':max(x['peak_sampled_rss_mb'] for x in v)} for k,v in runs.items()},'database_sha256_unchanged':original}

def equivalence(out,baseline='efb85bf2c532daa5e1ffed9ca6e0ec323009b359'):
    import test_automation_gateway as fixtures
    from retry_profile_smoke import install
    import automation_jobs
    fixtures.GatewayRegression.setUpClass();f=fixtures.GatewayRegression();f.setUp();install(f.win,'B');f.win.concurrency.setValue(2)
    try:p=automation_jobs.prepare(f.g.snapshot({}),{'mode':'trial','stores':['S1','S2']},lambda _:None,None)
    finally:f.tearDown()
    frozen=out/'frozen-input.pkl';frozen.write_bytes(pickle.dumps(p))
    archive=out/'baseline.zip'
    subprocess.run(['git','archive','--format=zip','-o',str(archive),baseline],cwd=ROOT,check=True)
    with zipfile.ZipFile(archive) as z:z.extractall(out/'baseline')
    script=out/'compare_worker.py';script.write_text("""import sys,pickle
from pathlib import Path
if __name__=='__main__':
    sys.path.insert(0,sys.argv[1]);import automation_jobs
    p=pickle.loads(Path(sys.argv[2]).read_bytes());results={}
    def notify(s,state,value,error):
        if value is not None:results[s]=value
        elif state=='计算失败':raise AssertionError(error)
    automation_jobs.calculate(p,p['codes'],notify,None)
    Path(sys.argv[3]).write_bytes(pickle.dumps({s:results[s] for s in p['codes']}))
""",'utf-8')
    outputs=[]
    for name,source in [('baseline',out/'baseline/src'),('candidate',ROOT/'src')]:
        dest=out/(name+'-results.pkl');subprocess.run([sys.executable,'-B',str(script),str(source),str(frozen),str(dest)],check=True);outputs.append(pickle.loads(dest.read_bytes()))
    def compare(a,b,path=''):
        if type(a) is not type(b):raise AssertionError('类型不同 '+path)
        if isinstance(a,dict):
            if list(a)!=list(b):raise AssertionError('字段或顺序不同 '+path)
            for k in a:compare(a[k],b[k],path+'/'+str(k))
        elif isinstance(a,(tuple,list)):
            if len(a)!=len(b):raise AssertionError('条数不同 '+path)
            for i,(x,y) in enumerate(zip(a,b)):compare(x,y,path+'/'+str(i))
        elif a!=b:raise AssertionError('值不同 '+path+': '+repr(a)+' / '+repr(b))
    # Explicit dynamic fields only; no row, quantity, amount or audit projection.
    excluded=['created','task.pid','task.started','task.finished','version.version']
    for result in outputs:
        for r in result.values():
            r.pop('created',None)
            for key in ('pid','started','finished'):r.get('task',{}).pop(key,None)
            r.get('version',{}).pop('version',None)
    compare(*outputs)
    return {'baseline':baseline,'input_sha256':hashlib.sha256(frozen.read_bytes()).hexdigest(),'excluded_top_level_fields':excluded,'difference_count':0,'scope':'2店2商品合成，预算10元，双进程，全部结果递归严格比较值类型和顺序；不代替真实全量业务验收'}

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--fixture-only',action='store_true');a=p.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    path,spec=fixture(out)
    if a.fixture_only:return
    performance=benchmark(out,path,spec);(out/'performance.json').write_text(json.dumps(performance,ensure_ascii=False,indent=2),'utf-8')
    result={'performance':performance,'equivalence':equivalence(out)}
    (out/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8');print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
