"""Generated Excel/SQLite fixture; two real application launches, source or EXE."""
import argparse,hashlib,json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests'),str(ROOT/'qa/export_071/qa-deps')]
import psutil,openpyxl,purchase_io,purchases,work_repository as wr
from test_archive_orders import seed

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--exe',type=Path);parser.add_argument('--output',type=Path,required=True);a=parser.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);work,repo,payload=seed(out);heads=purchases.targets(work.path);b=openpyxl.Workbook();s=b.active;s.append(purchase_io.FIELDS)
    for i,h in enumerate(heads):
        sku=next(iter(h['products']));r={'来源系统':'合成QA','采购订单号':f'P{i+1:03d}','采购明细号':'L001','下单日期':wr.today(),'门店编码':'T','供货仓':h['warehouse'],'商品编码':sku,'商品名称':'测试商品','本版实际下单数量':5 if i==0 else 3,'数量单位':'库存单位','补货单号':h['number'] if i else None,'签发修订':1 if i else None};s.append([r.get(f) for f in purchase_io.FIELDS])
    b.save(out/'purchase.xlsx');b.close();env=os.environ.copy()
    for key in ('PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','QT_QPA_PLATFORM'):env.pop(key,None)
    env['REPLENISHMENT_DATA_DIR']=str(out/'settings');env['PATH']=env['WINDIR']+';'+str(Path(env['WINDIR'])/'System32')
    if not a.exe:env['QT_QPA_PLATFORM']='offscreen'
    results=[]
    for phase in ('exercise','reopen'):
        command=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
        with (out/(phase+'.log')).open('w',encoding='utf-8') as log:
            process=subprocess.Popen(command+['--followup-smoke',str(out),phase],cwd=out,env=env,stdout=log,stderr=log);host=psutil.Process(process.pid);start=time.monotonic();children=set();peak=0
            while process.poll() is None:
                if time.monotonic()-start>180:
                    # Only this launcher-owned QA application/process tree.
                    for child in reversed(host.children(recursive=True)):child.terminate()
                    host.terminate();process.wait(10);raise TimeoutError('isolated UI acceptance timed out; owned process stopped')
                try:
                    tree=host.children(recursive=True);children.update((p.pid,p.create_time()) for p in tree);peak=max(peak,sum(p.memory_info().rss for p in [host]+tree))
                except psutil.Error:pass
                time.sleep(.05)
        report=json.loads((out/('ui-'+phase+'.json')).read_text(encoding='utf-8'));left=[]
        for pid,created in children:
            try:
                p=psutil.Process(pid)
                if p.create_time()==created and p.is_running():left.append(pid)
            except psutil.Error:pass
        report.update(exit_code=process.returncode,remaining_children=left,development=not bool(a.exe),exe_sha256=hashlib.sha256(a.exe.read_bytes()).hexdigest() if a.exe else None,elapsed_seconds=time.monotonic()-start,peak_tree_mib=peak/2**20,sample_ms=50,synthetic=True);(out/('ui-'+phase+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');results.append(report)
        if not report['passed'] or process.returncode or left:raise AssertionError(report)
    (out/'report.json').write_text(json.dumps({'passed':True,'runs':results},ensure_ascii=False,indent=2),encoding='utf-8');print('PASS actual EXE' if a.exe else 'PASS source UI')
if __name__=='__main__':main()
