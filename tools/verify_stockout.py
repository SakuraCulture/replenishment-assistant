"""Fresh isolated UI fixture and process-tree measurements. Never use business settings."""
import argparse,json,os,sys,subprocess,time,hashlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'qa/export_071/qa-deps')]


def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2),'utf-8')


def fixture(out):
    import openpyxl
    from stockout_common import file_hash,today
    from priority_list import make_profile
    out.mkdir(parents=True,exist_ok=False);settings=out/'settings';settings.mkdir();inputs=out/'inputs';inputs.mkdir()
    write(out/'synthetic.json',{'synthetic':True,'no_live_platform':True})
    write(settings/'store_supply.json',{s:{'primary':'河北仓','secondary':None} for s in ('S','T')})
    # ParameterScene persists routes in stores.ui, not the old standalone cache.
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(settings/'workbench.sqlite3')) as db,db:
        db.execute('CREATE TABLE stores(code TEXT PRIMARY KEY,data TEXT NOT NULL)')
        for code in ('S','T'):
            db.execute('INSERT INTO stores VALUES(?,?)',(code,json.dumps({'name':'测试门店'+code,'ui':{'primary':'山东仓','secondary':None}})))
    write(settings/'must_sell.json',make_profile([{'sku':s,'name':s,'category':'测试'} for s in ('A','L')],'隔离测试必卖清单'))
    def book(name,heads,rows,sheet_name=None):
        path=inputs/(name+'.xlsx');b=openpyxl.Workbook(write_only=True);w=b.create_sheet(title=sheet_name);w.append(heads)
        for row in rows:w.append(row)
        b.save(path);b.close();return {'file':str(path),'sha256':file_hash(path),'rows':len(rows)}
    tags=['标杆淘汰','淘便考核','标杆淘汰;淘便考核','','']
    catalog=book('山东仓',['商品编码','商品状态','可用数','分类','供应商名称','虚拟分类','基本售价','出库箱规'],
                 [[s,'禁用' if s=='C' else '启用',10,'饮品' if i%2 else '食品','供货商'+str(i%2),tags[i],2,1] for i,s in enumerate('ABCDE')])
    observed=today()+'T08:00:00+08:00';files=[]
    contract=json.loads((ROOT/'components/aoxiang/assets/field-contract-v2.json').read_text('utf-8'))
    heads=[c['rawHeader'] for c in contract['columns']];assert heads.count('毛利')==3
    for store,skus in [('S','ABCDEL'),('T','DEL')]:
        rows=[]
        for sku in skus:
            fields={'仓库/门店编码':store,'商品编码':sku,'商品名称':'测试商品'+sku,'月销':3,'可用库存':1 if sku in 'BE' else 0,
                    '在途库存':6,'补货状态':'停采' if sku=='C' else '正常'}
            rows.append([fields.get(h,index if h=='毛利' else None) for index,h in enumerate(heads)])
        value=book(store,heads,rows,contract['nativeSheetName'])
        files.append({**value,'store':store,'observed':observed})
    write(out/'ingest.json',{'day':today(),'catalogs':{'山东仓':catalog},'files':files})


def run(out,exe=None):
    import psutil
    command=[str(exe)] if exe else [sys.executable,str(ROOT/'src/app.py')]
    env={**os.environ,'REPLENISHMENT_DATA_DIR':str(out/'settings'),'PYTHONIOENCODING':'utf-8'}
    with (out/'process.log').open('w',encoding='utf-8') as log:
        start=time.monotonic();process=subprocess.Popen([*command,'--stockout-smoke',str(out)],cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        observed={};peak_tree=0;peak_children=0;temporary=0
        while process.poll() is None:
            try:tree=[psutil.Process(process.pid),*psutil.Process(process.pid).children(recursive=True)]
            except psutil.Error:tree=[]
            rss=child_rss=0
            for p in tree:
                try:
                    memory=p.memory_info().rss;rss+=memory
                    if p.pid!=process.pid:child_rss+=memory
                    io=p.io_counters();observed[p.pid]={'peak_rss':max(memory,observed.get(p.pid,{}).get('peak_rss',0)),
                        'read_bytes':io.read_bytes,'write_bytes':io.write_bytes,'cpu_seconds':sum(p.cpu_times()[:2])}
                except psutil.Error:pass
            peak_tree=max(peak_tree,rss);peak_children=max(peak_children,child_rss)
            temp=out/'settings/analysis/stockout/temporary'
            if temp.exists():
                size=0
                for p in temp.rglob('*'):
                    try:
                        if p.is_file():size+=p.stat().st_size
                    except OSError:pass
                temporary=max(temporary,size)
            if time.monotonic()-start>900:
                write(out/'still-running.json',{'pid':process.pid,'note':'Not terminated; inspect safe state before retry'});raise RuntimeError('Still running; no forced termination')
            time.sleep(.03)
    report=json.loads((out/'report.json').read_text('utf-8')) if (out/'report.json').exists() else {'passed':False}
    report.update(process_exit=process.returncode,seconds=time.monotonic()-start,tree_peak_rss=peak_tree,children_peak_rss=peak_children,
                  temporary_peak_bytes=temporary,processes=observed)
    report['application_peak_rss']=observed.get(report.get('application_pid'),{}).get('peak_rss')
    report['analysis_worker_peak_rss']=max((observed.get(r['pid'],{}).get('peak_rss',0) for r in report.get('workers',[])),default=0)
    if exe:
        with exe.open('rb') as f:report['exe_sha256']=hashlib.file_digest(f,'sha256').hexdigest()
    write(out/'report.json',report);print(json.dumps({k:report.get(k) for k in ('passed','process_exit','seconds','tree_peak_rss','children_peak_rss','error')},ensure_ascii=False))
    if not report['passed'] or process.returncode:raise SystemExit(1)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--exe',type=Path)
    args=p.parse_args();out=args.output.resolve();fixture(out);run(out,args.exe.resolve() if args.exe else None)
