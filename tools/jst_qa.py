"""Isolated 0.21 acceptance: fixture, alternating timings, exact EXE evidence."""
import argparse,copy,hashlib,json,os,shutil,sqlite3,statistics,subprocess,sys,time
from pathlib import Path
from contextlib import closing
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests'),str(ROOT/'tools'),str(ROOT/'qa/export_071/qa-deps')]


def save(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2),encoding='utf-8')
def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def fixture(out,count=2):
    import test_archive_orders as f,normal_orders as no,work_repository as wr,purchases,order_lifecycle as lc,warehouse_catalog as wc,openpyxl,lifecycle_io as io,export_settings
    out=Path(out);out.mkdir(parents=True,exist_ok=False);work,repo,payload=f.seed(out)
    with closing(sqlite3.connect(work.path)) as db,db:
        for i in range(1,count):
            p=copy.deepcopy(payload);store='S'+str(i);oid='oid-'+str(i);p['result']['store']=store;p['result']['store_name']='合成门店'+str(i)
            d=no.fingerprint(p['result']);db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(wr.today(),store,'normal_model',oid,'BH-'+store+'-ZC',1));db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',(oid,1,d,wr.now(),no.encode(p, db)))
    heads=lc.current_targets(work.path)
    for wh in wc.WAREHOUSES:
        path=out/(wh+'-分配.xlsx');b=openpyxl.Workbook();s=b.active;s.append(wc.FIELDS)
        for sku in ('0001','0002'):
            row={'商品编码':sku,'商品名称':'合成商品'+sku,'分类':'快消','基本售价':2.5,'出库箱规':24,'商品状态':'启用','可用数':36*count}
            s.append([row.get(k) for k in wc.FIELDS])
        b.save(path);b.close();wc.Repository(out/'settings').save(wc.import_catalog(path,wh))
        b=openpyxl.load_workbook(path);s=b.active;stock_col=wc.FIELDS.index('可用数')+1
        for row in range(2,s.max_row+1):s.cell(row,stock_col,24)
        b.save(out/(wh+'-新库存.xlsx'));b.close()
    book=openpyxl.Workbook();ws=book.active;ws.title='门店回传单';ws.append(io.fields('return'))
    for h in heads:
        sku=next(iter(h['products']));r={'补货单号':h['number'],'门店编码':h['store'],'供货仓':h['warehouse'],'商品编码':sku,'商品名称':'合成商品'+sku,'门店确认数量':48,'数量单位':'库存单位','回传单号':h['number']}
        ws.append([r.get(k) for k in io.fields('return')])
    path=out/'returns.xlsx';book.save(path);book.close();info=io.inspect([path])[0];sheet=info['sheets'][0]
    scan=io.scan('return',[{**info,**sheet,'mapping':{h:i for i,h in enumerate(sheet['headers'])}}]);lc.commit(work.path,lc.prepare_numbered(work.path,scan))
    (out/'exports').mkdir();export_settings.save(out/'settings/export_settings.json',str(out/'exports'))
    save(out/'fixture.json',{'synthetic':True,'stores':count,'orders':len(heads),'heads':heads,'database':str(work.path)})
    return work.path


def protect(qa):
    targets=[ROOT/'交付_0.20.0_rc1',ROOT/'docs/plans/跨系统自动化闭环待办开发方案_20261031.md']
    values={}
    for p in targets:
        if p.is_dir():values.update({str(f):sha(f) for f in p.rglob('*') if f.is_file()})
        elif p.is_file():values[str(p)]=sha(p)
    target=qa/'additional-protection.json'
    if target.exists():raise FileExistsError(target)
    save(target,values);print('Protected',len(values))


def worker(source,dbpath,out,mode):
    # Import only the selected source; no accidental candidate modules in baseline capture.
    sys.path=[str(source/'src')]+[p for p in sys.path if p!=str(ROOT/'src')]
    import lifecycle_home,order_lifecycle as lc
    q={};start=time.perf_counter()
    v=lifecycle_home.read(dbpath,out/'index.sqlite3',{})
    q['query_cold']=time.perf_counter()-start;start=time.perf_counter();lifecycle_home.read(dbpath,out/'index.sqlite3',{});q['query_warm']=time.perf_counter()-start
    heads=lc.current_targets(dbpath);start=time.perf_counter();lc.export(dbpath,heads,out/'exports','comparison');q['original_export']=time.perf_counter()-start
    if mode=='new':
        import jst_orders as j,jst_exports as ex,warehouse_catalog as wc,daily_batch
        start=time.perf_counter()
        for wh,c in wc.Repository(Path(dbpath).parent).catalogs().items():j.commit_refresh(dbpath,j.prepare_refresh(dbpath,wh,catalog=c))
        q['catalog_prepare_save']=time.perf_counter()-start;start=time.perf_counter();p=j.prepare(dbpath,heads);q['allocation_preview']=time.perf_counter()-start
        start=time.perf_counter();r=j.commit(dbpath,p);q['generation_save']=time.perf_counter()-start
        if r['errors']:raise ValueError(r)
        start=time.perf_counter();export=ex.export(dbpath,r['generated'],out/'jst');q['jst_export']=time.perf_counter()-start
        if export['failed']:raise ValueError(export)
        start=time.perf_counter();daily_batch.read(dbpath,out/'index.sqlite3',{});q['daily_query']=time.perf_counter()-start
        q['generated']=len(r['generated']);q['amount']=str(sum(__import__('decimal').Decimal(x['summary']['amount']) for x in p['payload']['plans']))
    save(out/'timings.json',q)


def measure(cmd,out,env,timeout=600):
    import psutil
    start=time.perf_counter();peak=0;children=set()
    with (out/'process.log').open('w',encoding='utf-8') as log:
        proc=subprocess.Popen(cmd,cwd=ROOT,env=env,stdout=log,stderr=log);host=psutil.Process(proc.pid)
        while proc.poll() is None:
            if time.perf_counter()-start>timeout:raise TimeoutError(f'Owned task still running PID {proc.pid}; do not kill during commit')
            try:
                tree=host.children(recursive=True);children.update((c.pid,c.create_time()) for c in tree);peak=max(peak,sum(c.memory_info().rss for c in [host]+tree))
            except psutil.Error:pass
            time.sleep(.05)
    left=[]
    for pid,created in children:
        try:
            c=psutil.Process(pid)
            if c.create_time()==created and c.is_running():left.append(pid)
        except psutil.Error:pass
    result={'exit_code':proc.returncode,'elapsed':time.perf_counter()-start,'peak_tree_mib':peak/2**20,'sample_ms':50,'remaining_children':left}
    save(out/'process.json',result)
    if proc.returncode or left:raise RuntimeError(str(out/'process.log'))
    return result


def benchmark(qa,out):
    out.mkdir(parents=True,exist_ok=False);results=[]
    env=os.environ.copy();env.update(PYTHONHASHSEED='0',PYTHONIOENCODING='utf-8',QT_QPA_PLATFORM='offscreen')
    for stores in (3,10,50):
        seed=out/f'seed-{stores}';fixture(seed,stores)
        for repeat in range(3):
            for mode in (('old','new') if repeat%2==0 else ('new','old')):
                target=out/f'{stores}-{repeat}-{mode}';shutil.copytree(seed,target);source=qa/'baseline' if mode=='old' else ROOT
                run=measure([sys.executable,__file__,'worker','--source',str(source),'--out',str(target),'--mode',mode],target,env)
                results.append({'stores':stores,'orders':stores*2,'repeat':repeat,'mode':mode,**run,**json.loads((target/'timings.json').read_text(encoding='utf-8'))})
                save(out/'raw.json',results)
    summary=[]
    for stores in (3,10,50):
        for mode in ('old','new'):
            group=[r for r in results if r['stores']==stores and r['mode']==mode]
            metrics={k:{'median':statistics.median(r[k] for r in group),'min':min(r[k] for r in group),'max':max(r[k] for r in group)} for k in group[0] if k not in ('repeat','stores','orders','mode','remaining_children','amount') and isinstance(group[0][k],(float,int))}
            summary.append({'stores':stores,'mode':mode,'metrics':metrics})
    save(out/'report.json',{'synthetic':True,'common_operations':'same signed/return fixture, sequential alternating runs; cold means first projection in fresh process, OS file cache not flushed','new_operations':'no baseline counterpart, report absolute times only','runs':results,'summary':summary})


def exe(out,program=None):
    fixture(out);env=os.environ.copy()
    for k in ('PYTHONPATH','PYTHONHOME','QT_QPA_PLATFORM','QT_PLUGIN_PATH'):env.pop(k,None)
    env.update(REPLENISHMENT_DATA_DIR=str(out/'settings'),PYTHONHASHSEED='0',PYTHONIOENCODING='utf-8')
    if program:env['PATH']=env['WINDIR']+';'+str(Path(env['WINDIR'])/'System32')
    reports=[]
    for phase in ('exercise','reopen','close_retry'):
        case=out if phase!='close_retry' else out/'close-test'
        if phase=='close_retry':fixture(case)
        env['REPLENISHMENT_DATA_DIR']=str(case/'settings')
        record=case/phase;record.mkdir()
        cmd=[str(program.resolve())] if program else [sys.executable,str(ROOT/'src/app.py')]
        result=measure(cmd+['--jst-smoke',str(case),phase],record,env)
        evidence=json.loads((case/('ui-'+phase+'.json')).read_text(encoding='utf-8'))
        result.update(evidence)
        if not result['passed']:raise AssertionError(evidence)
        reports.append(result)
    save(out/'report.json',{'passed':True,'actual_exe':bool(program),'exe_sha256':sha(program) if program else None,'runs':reports})


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['protect','benchmark','worker','exe','fixture']);p.add_argument('--qa',type=Path,default=ROOT/'qa/jst_0210');p.add_argument('--out',type=Path);p.add_argument('--source',type=Path);p.add_argument('--mode');p.add_argument('--exe',type=Path);a=p.parse_args()
    if a.action=='protect':protect(a.qa.resolve())
    elif a.action=='fixture':fixture(a.out.resolve())
    elif a.action=='benchmark':benchmark(a.qa.resolve(),a.out.resolve())
    elif a.action=='worker':
        out=a.out.resolve();worker(a.source.resolve(),out/'settings/workbench.sqlite3',out,a.mode)
    else:exe(a.out.resolve(),a.exe)


if __name__=='__main__':main()
