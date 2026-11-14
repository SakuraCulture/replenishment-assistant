"""New synthetic settings only: real Qt business loops, normal exit and reopen."""
import argparse,ctypes,hashlib,json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src')]


def fixture(out,rounds):
    import openpyxl,warehouse_catalog as wc,work_repository as wr,parameter_schema as ps,store_defaults,new_product_focus
    raw=out/'inputs';raw.mkdir();settings=out/'settings';repo=wr.Repository(settings)
    stores=['LIFETIME_A','LIFETIME_B']
    def book(path,heads,rows):
        b=openpyxl.Workbook();s=b.active;s.title='商品资料' if path.name!='sales.xlsx' else '商品补货建议'
        s.append(heads)
        for row in rows:s.append(row)
        b.save(path);b.close()
    heads=['仓库/门店编码','仓库/门店名称','商品编码','商品名称','日销','周销','月销','可用库存','在途库存','缺货天数（天）','补货状态','基础单位','商品标签']
    rows=[[store,'合成生命周期门店'+str(n+1),str(10000+i),'合成商品'+str(i),1+i/10,7+i,30+i,i%3,0,0,'正常','瓶',''] for n,store in enumerate(stores) for i in range(12)]
    book(raw/'sales.xlsx',heads,rows)
    catalog=wc.Repository(settings)
    for wh in wc.WAREHOUSES:
        path=raw/(wh+'.xlsx')
        book(path,['商品编码','商品名称','分类','基本售价','出库箱规','商品状态','可用数','商品标签'],[[str(10000+i),'合成商品'+str(i),'饮品',2+i/10,2,'启用',500,''] for i in range(12)])
        catalog.save(wc.import_catalog(path,wh))
    config=ps.standard();preset=repo.presets()[0]
    repo.save_stores({store:store_defaults.record(preset,config,{'mode':1,'amount':'','primary':'河北仓','secondary':'山东仓','selected':True},include_budget=True,saved=wr.now()) for store in stores})
    new_product_focus.Repository(settings).save(new_product_focus.empty(wr.today()[:7]))
    (out/'fixture.json').write_text(json.dumps({'synthetic':True,'live_platform':False,'stores':stores,'rounds':rounds,'default_concurrency':2},ensure_ascii=False,indent=2),'utf-8')


def rss(pid):
    if os.name!='nt':return 0
    from ctypes import wintypes as w
    class Memory(ctypes.Structure):
        _fields_=[('cb',w.DWORD),('PageFaultCount',w.DWORD),('PeakWorkingSetSize',ctypes.c_size_t),('WorkingSetSize',ctypes.c_size_t),('QuotaPeakPagedPoolUsage',ctypes.c_size_t),('QuotaPagedPoolUsage',ctypes.c_size_t),('QuotaPeakNonPagedPoolUsage',ctypes.c_size_t),('QuotaNonPagedPoolUsage',ctypes.c_size_t),('PagefileUsage',ctypes.c_size_t),('PeakPagefileUsage',ctypes.c_size_t)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True);kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];kernel.OpenProcess.restype=w.HANDLE;kernel.CloseHandle.argtypes=[w.HANDLE]
    h=kernel.OpenProcess(0x1000|0x10,False,pid)
    if not h:return 0
    try:
        m=Memory();m.cb=ctypes.sizeof(m);psapi=ctypes.WinDLL('psapi');psapi.GetProcessMemoryInfo.argtypes=[w.HANDLE,ctypes.POINTER(Memory),w.DWORD]
        return m.WorkingSetSize if psapi.GetProcessMemoryInfo(h,ctypes.byref(m),m.cb) else 0
    finally:kernel.CloseHandle(h)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--out',type=Path,required=True);parser.add_argument('--exe',type=Path)
    parser.add_argument('--native-source',action='store_true');parser.add_argument('--rounds',type=int,default=20);parser.add_argument('--timeout',type=float,default=300)
    args=parser.parse_args();out=args.out.resolve();out.mkdir(parents=True,exist_ok=False)
    if not 1<=args.rounds<=100:raise ValueError('rounds must be 1..100')
    if os.name=='nt':
        from application_processes import discover
        active=[r for r in discover(guarded=True) if r['main']]
        if active:
            (out/'blocked-by-active-instance.json').write_text(json.dumps(active,ensure_ascii=False,indent=2),'utf-8')
            raise RuntimeError('A real application instance is active; not terminated or bypassed: '+','.join(str(r['pid']) for r in active))
    (out/'synthetic.json').write_text(json.dumps({'synthetic':True,'live_platform':False,'purpose':'runtime-Qt-lifetime-acceptance'}),'utf-8')
    fixture(out,args.rounds)
    env=os.environ.copy()
    for key in ('PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','QT_QPA_PLATFORM','REPLENISHMENT_DATA_DIR','REPLENISHMENT_PORTABLE_PATHS'):env.pop(key,None)
    env.update(PYTHONUTF8='1',PYTHONIOENCODING='utf-8',REPLENISHMENT_DATA_DIR=str(out/'settings'),LOCALAPPDATA=str(out/'localappdata'),APPDATA=str(out/'roaming'))
    if not args.exe and not args.native_source:env['QT_QPA_PLATFORM']='offscreen'
    base=[str(args.exe.resolve())] if args.exe else [sys.executable,str(ROOT/'src/app.py')]
    report={'passed':False,'live_platform':False,'synthetic':True,'runs':[],'exe_sha256':hashlib.sha256(args.exe.read_bytes()).hexdigest() if args.exe else None}
    for mode in ('exercise','reopen'):
        started=time.monotonic();peak=0;pids=set()
        with (out/(mode+'-process.log')).open('w',encoding='utf-8') as log:
            process=subprocess.Popen(base+['--runtime-lifetime-smoke',str(out),mode],cwd=out,env=env,stdout=log,stderr=log)
            while process.poll() is None:
                if time.monotonic()-started>args.timeout:
                    report.update(timeout=True,pid=process.pid,message='Owned QA process remains running; no forced termination during unknown write')
                    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');raise TimeoutError(report['message'])
                if os.name=='nt':
                    from application_processes import process_snapshot
                    snap=process_snapshot();tree={process.pid}
                    while True:
                        enlarged=tree|{pid for pid,r in snap.items() if r['parent'] in tree}
                        if enlarged==tree:break
                        tree=enlarged
                    pids|=tree;peak=max(peak,sum(rss(pid) for pid in tree))
                time.sleep(.05)
        result_file=out/(mode+'-report.json')
        run=json.loads(result_file.read_text('utf-8')) if result_file.exists() else {'passed':False,'error':'Native acceptance report missing'}
        log_lines=(out/(mode+'-process.log')).read_text('utf-8',errors='replace').splitlines()
        run['qt_model_or_lifetime_warnings']=[line for line in log_lines if any(token in line for token in ('beginResetModel','endResetModel','QObject:','QThread:','Must construct a QApplication','pure virtual'))]
        if run['qt_model_or_lifetime_warnings']:
            run.update(passed=False,error='Qt model or lifetime warnings require investigation: '+run['qt_model_or_lifetime_warnings'][0])
        run.update(exit_code=process.returncode,supervised_seconds=time.monotonic()-started,peak_owned_tree_mib=peak/2**20,memory_method='50 ms sum of owned main/child working sets; shared pages counted in each process')
        result_file.write_text(json.dumps(run,ensure_ascii=False,indent=2),'utf-8');report['runs'].append(run)
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
        if process.returncode or not run['passed']:raise AssertionError(run.get('error') or run)
    report.update(passed=True,checks=sum(len(r['checks']) for r in report['runs']))
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps({'passed':True,'checks':report['checks'],'actual_exe':bool(args.exe),'report':str(out/'report.json')},ensure_ascii=False))


if __name__=='__main__':main()
