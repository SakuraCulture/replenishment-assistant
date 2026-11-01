"""Generate own inputs/settings, launch exercise + reopen; never user data."""
import argparse,json,os,shutil,subprocess,sys,time,hashlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tools'),str(ROOT/'tests'),str(ROOT/'qa/export_071/qa-deps')]
import psutil

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--exe',type=Path);ap.add_argument('--native-source',action='store_true');ap.add_argument('--output',type=Path,required=True);a=ap.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    import warehouse_catalog as wc,work_repository as wr,parameter_schema as ps,store_defaults,new_product_focus,export_settings
    (out/'exports').mkdir();repo=wr.Repository(out/'settings');export_settings.save(out/'settings/export_settings.json',str(out/'exports'))
    source=ROOT/'qa/preissue_0180/inputs'
    for name in ('sales.xlsx','河北仓.xlsx','山东仓.xlsx'):shutil.copy2(source/name,out/name)
    # Extend, never modify, the established fixtures: one extra synthetic SKU
    # in both stores/catalogs exercises positive new-product money in the EXE.
    import openpyxl
    for name in ('sales.xlsx','河北仓.xlsx','山东仓.xlsx'):
        path=out/name;book=openpyxl.load_workbook(path);ws=book.active;headers=[c.value for c in ws[1]]
        sku=headers.index('商品编码');title=headers.index('商品名称')
        prototypes=[list(r) for r in ws.iter_rows(min_row=2,values_only=True) if r[sku]=='both']
        for values in prototypes:
            values[sku]='qa_new_amount';values[title]='合成新增-新品金额验收'
            if name=='sales.xlsx':
                for field in ('日销','周销','月销','可用库存','在途库存','缺货天数（天）'):values[headers.index(field)]=0
            ws.append(values)
        book.save(path);book.close()
    catalog=wc.Repository(out/'settings')
    for wh in wc.WAREHOUSES:catalog.save(wc.import_catalog(out/(wh+'.xlsx'),wh))
    preset=repo.presets()[0];config=ps.standard()
    repo.save_stores({s:store_defaults.record(preset,config,{'mode':1,'amount':'','primary':p,'secondary':q,'selected':True},include_budget=True,saved=wr.now()) for s,p,q in [('QA_A','河北仓','山东仓'),('QA_B','山东仓','河北仓')]})
    new_product_focus.Repository(out/'settings').save(new_product_focus.empty(wr.today()[:7]))
    env=os.environ.copy()
    for k in ('PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','QT_QPA_PLATFORM'):env.pop(k,None)
    env.update(REPLENISHMENT_DATA_DIR=str(out/'settings'),PATH=env['WINDIR']+';'+str(Path(env['WINDIR'])/'System32'),PYTHONIOENCODING='utf-8')
    if not a.exe and not a.native_source:env['QT_QPA_PLATFORM']='offscreen'
    reports=[]
    for phase in ('exercise','reopen'):
        cmd=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
        with (out/(phase+'.log')).open('w',encoding='utf-8') as log:
            proc=subprocess.Popen(cmd+['--preissue-smoke',str(out),phase],cwd=out,env=env,stdout=log,stderr=log);host=psutil.Process(proc.pid);start=time.monotonic();children=set();peak=0
            while proc.poll() is None:
                # Do not kill an unknown transaction at a timeout: report the owned PID.
                if time.monotonic()-start>240:raise TimeoutError(f'QA still running: owned PID {proc.pid}; inspect before cancelling')
                try:
                    tree=host.children(recursive=True);children.update((c.pid,c.create_time()) for c in tree);peak=max(peak,sum(c.memory_info().rss for c in [host]+tree))
                except psutil.Error:pass
                time.sleep(.05)
        r=json.loads((out/('ui-'+phase+'.json')).read_text(encoding='utf-8'));left=[]
        for pid,created in children:
            try:
                c=psutil.Process(pid)
                if c.create_time()==created and c.is_running():left.append(pid)
            except psutil.Error:pass
        r.update(exit_code=proc.returncode,remaining_children=left,source_only=not bool(a.exe),elapsed_seconds=time.monotonic()-start,peak_tree_mib=peak/2**20,sample_ms=50,exe_sha256=hashlib.sha256(a.exe.read_bytes()).hexdigest() if a.exe else None)
        (out/('ui-'+phase+'.json')).write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8');reports.append(r)
        if not r['passed'] or proc.returncode or left:raise AssertionError(r)
    (out/'report.json').write_text(json.dumps({'passed':True,'runs':reports},ensure_ascii=False,indent=2),encoding='utf-8');print('PASS actual EXE' if a.exe else 'PASS source UI')
if __name__=='__main__':main()
