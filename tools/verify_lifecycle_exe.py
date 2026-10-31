"""Two actual app processes, generated isolated documents, native UI/spawn checks."""
import argparse,hashlib,json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests'),str(ROOT/'qa/export_071/qa-deps')]
import psutil,openpyxl,lifecycle_io as io,purchases,work_repository as wr
from test_archive_orders import seed

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--exe',type=Path);parser.add_argument('--output',type=Path,required=True);a=parser.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);work,repo,payload=seed(out);heads=purchases.targets(work.path)
    for stage in io.STAGES:
        for suffix in ('', '-v2') if stage=='return' else ('','-invalid') if stage=='receipt' else ('',):
            b=openpyxl.Workbook();s=b.active;s.append(io.fields(stage))
            if stage=='return':
                s.title='往期新品'
                for name in ('本月新品','中心仓动销补货清单'):b.create_sheet(name).append(io.fields(stage))
            for i,h in enumerate(heads):
                sku=next(iter(h['products']));r={'补货单号':h['number'],'签发修订':None if stage=='return' else 1,'门店编码':'T','供货仓':h['warehouse'],'商品编码':sku,'商品名称':'合成商品','数量单位':'库存单位',io.QUANTITIES[stage]:None if suffix=='-invalid' else 2 if suffix=='-v2' else 5,io.DOCNUM[stage]:f'D{i+1:03d}',io.DOCDATE[stage]:wr.today(),'实际金额':12,'币种':'CNY'};(b.worksheets[i+1] if stage=='return' else s).append([r.get(f) for f in io.fields(stage)])
            b.save(out/(stage+suffix+'.xlsx'));b.close()
    env=os.environ.copy()
    for key in ('PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','QT_QPA_PLATFORM'):env.pop(key,None)
    env['REPLENISHMENT_DATA_DIR']=str(out/'settings');env['PATH']=env['WINDIR']+';'+str(Path(env['WINDIR'])/'System32')
    if not a.exe:env['QT_QPA_PLATFORM']='offscreen'
    results=[]
    for phase in ('exercise','reopen'):
        command=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
        with (out/(phase+'.log')).open('w',encoding='utf-8') as log:
            process=subprocess.Popen(command+['--lifecycle-smoke',str(out),phase],cwd=out,env=env,stdout=log,stderr=log);host=psutil.Process(process.pid);start=time.monotonic();children=set();peak=0
            while process.poll() is None:
                if time.monotonic()-start>180:
                    for child in reversed(host.children(recursive=True)):child.terminate()
                    host.terminate();process.wait(10);raise TimeoutError('isolated lifecycle UI acceptance timed out; owned process stopped')
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
