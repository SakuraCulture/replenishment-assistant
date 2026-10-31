"""Two fresh application processes; all settings, templates and outputs private."""
import argparse,json,os,shutil,subprocess,sys,time,hashlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tools'),str(ROOT/'tests'),str(ROOT/'qa/export_071/qa-deps')]
import psutil
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--exe',type=Path);ap.add_argument('--output',type=Path,required=True);a=ap.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    from verify_reconciliation_exe import fixture
    import warehouse_catalog as wc,work_repository as wr,parameter_schema as ps,store_defaults,new_product_focus,export_settings
    fixture(out);exports=out/'exports';exports.mkdir();export_settings.save(out/'settings/export_settings.json',str(exports))
    old=ROOT/'qa/structure_061/inputs'
    for name in ('sales.xlsx','河北仓.xlsx','山东仓.xlsx'):shutil.copy2(old/name,out/name)
    catalog=wc.Repository(out/'settings')
    for wh in wc.WAREHOUSES:catalog.save(wc.import_catalog(out/(wh+'.xlsx'),wh))
    repo=wr.Repository(out/'settings');preset=repo.presets()[0];config=ps.standard()
    repo.save_stores({s:store_defaults.record(preset,config,{'mode':1,'amount':'','primary':p,'secondary':q,'selected':True},include_budget=True,saved=wr.now()) for s,p,q in [('QA_A','河北仓','山东仓'),('QA_B','山东仓','河北仓')]})
    new_product_focus.Repository(out/'settings').save(new_product_focus.empty(wr.today()[:7]))
    env=os.environ.copy()
    for k in ('PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','QT_QPA_PLATFORM'):env.pop(k,None)
    env['REPLENISHMENT_DATA_DIR']=str(out/'settings');env['PATH']=env['WINDIR']+';'+str(Path(env['WINDIR'])/'System32');env['PYTHONIOENCODING']='utf-8'
    if not a.exe:env['QT_QPA_PLATFORM']='offscreen'
    reports=[]
    for phase in ('exercise','reopen'):
        cmd=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
        with (out/(phase+'.log')).open('w',encoding='utf-8') as log:
            p=subprocess.Popen(cmd+['--business-flows-smoke',str(out),phase],cwd=out,env=env,stdout=log,stderr=log);host=psutil.Process(p.pid);start=time.monotonic();children=set();peak=0
            while p.poll() is None:
                if time.monotonic()-start>240:
                    for child in reversed(host.children(recursive=True)):child.terminate()
                    host.terminate();p.wait(10);raise TimeoutError('owned QA process exceeded timeout')
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
        r.update(exit_code=p.returncode,remaining_children=left,source_only=not bool(a.exe),elapsed_seconds=time.monotonic()-start,peak_tree_mib=peak/2**20,sample_ms=50,exe_sha256=hashlib.sha256(a.exe.read_bytes()).hexdigest() if a.exe else None)
        (out/('ui-'+phase+'.json')).write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8');reports.append(r)
        if not r['passed'] or p.returncode or left:raise AssertionError(r)
    (out/'report.json').write_text(json.dumps({'passed':True,'runs':reports},ensure_ascii=False,indent=2),encoding='utf-8');print('PASS actual EXE' if a.exe else 'PASS source UI')
if __name__=='__main__':main()
