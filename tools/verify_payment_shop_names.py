"""New-folder synthetic EXE maintenance, import, review, repeat and reopen."""
import argparse,hashlib,json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/p) for p in ('src','tests','tools')]

def fixture(out):
    from verify_payment_matching import fixture as base
    import jst_orders as jst,openpyxl
    path=base(out);info=json.loads((out/'matching-fixture.json').read_text('utf-8'));g=jst.get(path,info['gids'][0])
    for filename,internal,name in [('唯一名称差异付款','NEW-UNIQUE','隔离独立仓店名'),('两仓同名付款','NEW-BOTH','隔离两仓同名'),('两仓新单','NEXT-BOTH','隔离两仓同名')]:
        w=openpyxl.Workbook();s=w.active;s.append(['内部订单号','店铺名称','商品编码','数量','商品金额'])
        for r in g['rows']:
            if float(r['quantity'])>0:s.append([internal,name,r['sku'],7,'8.25'])
        w.save(out/(filename+'.xlsx'));w.close()
    (out/'shop-fixture.json').write_text(json.dumps({'head':info['head'],'synthetic':True,'live_platform':False},ensure_ascii=False),'utf-8')
    return path

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',required=True,type=Path);p.add_argument('--exe',type=Path);a=p.parse_args();out=a.out.resolve()
    if out.exists():raise FileExistsError('Only new isolated acceptance folders are allowed')
    fixture(out)
    env={**os.environ,'REPLENISHMENT_DATA_DIR':str(out/'settings'),'LOCALAPPDATA':str(out/'localappdata'),'PYTHONUTF8':'1','QT_QPA_PLATFORM':'windows'}
    cmd=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    reports=[]
    for phase in ('exercise','reopen'):
        with (out/('process-'+phase+'.log')).open('wb') as log:
            proc=subprocess.Popen(cmd+['--payment-shop-names-smoke',str(out),phase],cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW);start=time.monotonic()
            while proc.poll() is None:
                if time.monotonic()-start>150:raise TimeoutError(f'Acceptance timed out; process {proc.pid} retained, no forced termination')
                time.sleep(.2)
        report=json.loads((out/('report-'+phase+'.json')).read_text('utf-8'));report['process_exit']=proc.returncode;reports.append(report)
        if not report['passed'] or proc.returncode:break
    result={'passed':len(reports)==2 and all(r['passed'] and r['process_exit']==0 for r in reports),'reports':reports,'live_platform':False}
    if a.exe:
        with a.exe.open('rb') as f:result['exe_sha256']=hashlib.file_digest(f,'sha256').hexdigest()
    (out/'acceptance.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8');print(json.dumps(result,ensure_ascii=False));return 0 if result['passed'] else 1
if __name__=='__main__':sys.exit(main())
