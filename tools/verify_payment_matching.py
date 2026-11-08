"""Isolated fixture + source/EXE payment matching acceptance. No live services."""
import argparse,json,os,subprocess,sys,time,hashlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tools')]

def fixture(out,count=2):
    from jst_qa import fixture as seed
    import jst_orders as j,warehouse_catalog as wc,order_lifecycle as lc,openpyxl
    path=seed(out,count)
    for wh,c in wc.Repository(path.parent).catalogs().items():j.commit_refresh(path,j.prepare_refresh(path,wh,catalog=c))
    gids=j.commit(path,j.prepare(path,lc.current_targets(path)))['generated'];g=j.get(path,gids[0])
    folder=path.parent/'jst_catalog_sync';folder.mkdir(exist_ok=True)
    (folder/'jobs.json').write_text(json.dumps({'accounts':{'sd':{'id':'sd','warehouse':'山东仓','expectedCoid':'100','expectedUid':'1','mappingVerified':True,'expectedCompany':'隔离合成来源企业','bindingVersion':1}}},ensure_ascii=False),'utf-8')
    file=out/'付款合成.xlsx';b=openpyxl.Workbook();s=b.active;s.append(['内部订单号','商品编码','数量','商品金额','店铺名称','买家帐号','下单时间'])
    for r in g['rows']:
        if float(r['quantity'])>0:s.append(['J-OFFLINE',r['sku'],r['quantity'],r['amount'],g['head']['name'],'合成帐号',g['head']['day']+' 12:00:00'])
    b.save(file);b.close()
    cases=[]
    for name,number,internal,alias,amount_delta in [
        ('别称首次自动识别',None,'J-ALIAS','不同店名（业务别称）',0),
        ('金额更正自动认原单',None,'J-OFFLINE',g['head']['name'],.01),
        ('新增签发单号',g['head']['number'],'J-EXPLICIT',g['head']['name'],0),
        ('签发单号填错',j.get(path,gids[-1])['head']['number'],'J-WRONG',g['head']['name'],0)]:
        from decimal import Decimal
        f=out/(name+'.xlsx');book=openpyxl.Workbook();sheet=book.active;sheet.append(['内部订单号','商品编码','数量','商品金额','店铺名称','签发单号'])
        first=True
        for r in g['rows']:
            if Decimal(r['quantity'])<=0:continue
            sheet.append([internal,r['sku'],r['quantity'],str(Decimal(r['amount'])+Decimal(str(amount_delta if first else 0))),alias,number]);first=False
        book.save(f);book.close();cases.append({'name':name,'path':str(f),'expected_number':None if name=='签发单号填错' else g['head']['number'],'reason':'签发单号与明细冲突' if name=='签发单号填错' else '', 'screenshot':'自动认单_'+name+'.png'})
    (out/'matching-fixture.json').write_text(json.dumps({'payment':str(file),'head':g['head'],'gids':gids,'automatic_cases':cases,'synthetic':True,'live_platform':False},ensure_ascii=False),'utf-8')
    return path

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);p.add_argument('--exe',type=Path);a=p.parse_args()
    out=a.out.resolve();out.relative_to(ROOT/'qa');fixture(out)
    env={**os.environ,'REPLENISHMENT_DATA_DIR':str(out/'settings'),'LOCALAPPDATA':str(out/'localappdata'),'PYTHONUTF8':'1','QT_QPA_PLATFORM':'windows'}
    cmd=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    with (out/'process.log').open('wb') as log:
        proc=subprocess.Popen(cmd+['--payment-matching-smoke',str(out)],cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW);started=time.monotonic()
        while proc.poll() is None:
            if time.monotonic()-started>180:raise TimeoutError(f'Isolated process {proc.pid} retained; no forced termination')
            time.sleep(.2)
    report=json.loads((out/'report.json').read_text('utf-8'));report['process_exit']=proc.returncode
    if a.exe:
        with a.exe.open('rb') as f:report['exe_sha256']=hashlib.file_digest(f,'sha256').hexdigest()
    (out/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps(report,ensure_ascii=False));return 0 if report['passed'] and proc.returncode==0 else 1
if __name__=='__main__':sys.exit(main())
