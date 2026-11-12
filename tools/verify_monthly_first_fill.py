"""Owned actual-header fixture for the native calculate -> export workflow."""
import argparse,json,os,sys,time,subprocess,hashlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/p) for p in ('src','tests')]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--out',type=Path,required=True);parser.add_argument('--exe',type=Path);args=parser.parse_args()
    out=args.out.resolve();out.relative_to(ROOT/'qa');out.mkdir(parents=True,exist_ok=False)
    for folder in ('inputs','settings','exports'):(out/folder).mkdir()
    import openpyxl,batch_service,warehouse_catalog,new_product_focus,priority_list,work_repository as wr
    from test_monthly_first_fill import MonthlyFirstFill,monthly
    fixture=MonthlyFirstFill();fixture.setUp()
    try:fixture.root=out/'inputs';_,catalogs=fixture.load()
    finally:fixture.doCleanups()
    sales=out/'inputs/sales.xlsx';book=openpyxl.Workbook();sheet=book.active
    sheet.append(['仓库/门店编码','仓库/门店名称','商品编码','商品名称','日销','周销','月销','可用库存','在途库存','缺货天数（天）','补货状态'])
    sheet.append(['S1','合成店','0001','合成新品',0,0,0,0,0,0,'正常']);book.save(sales);book.close()
    loaded=batch_service.load([sales],catalogs);settings=out/'settings';repo=warehouse_catalog.Repository(settings)
    for catalog in catalogs.values():repo.save(catalog)
    repo.save_route('S1',{'primary':'山东仓','secondary':None})
    profile=monthly(wr.today()[:7]);new_product_focus.Repository(settings).save(profile)
    priority_list.Repository(settings).save(priority_list.make_profile([{'sku':'9999','name':'隔离必采'}],'隔离必采清单'))
    data={'synthetic':True,'live_platform':False,'day':wr.today(),'monthly':profile,'loaded':loaded}
    (out/'fixture.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8',newline='\n')
    (out/'synthetic.json').write_text(json.dumps({'synthetic':True,'live_platform':False}),encoding='utf-8')
    command=[str(args.exe.resolve())] if args.exe else [sys.executable,str(ROOT/'src/app.py')]
    env={**os.environ,'PYTHONUTF8':'1','QT_QPA_PLATFORM':'windows','REPLENISHMENT_DATA_DIR':str(settings),'LOCALAPPDATA':str(out/'localappdata')}
    with (out/'process.log').open('wb') as log:
        process=subprocess.Popen(command+['--monthly-first-fill-smoke',str(out)],cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW);start=time.monotonic()
        while process.poll() is None:
            if time.monotonic()-start>180:raise TimeoutError(f'Owned QA process {process.pid} retained; no forced termination')
            time.sleep(.2)
    report=json.loads((out/'report.json').read_text('utf-8'));report['process_exit']=process.returncode
    if args.exe:report['exe_sha256']=hashlib.sha256(args.exe.read_bytes()).hexdigest()
    (out/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps({'passed':report['passed'],'checks':len(report['checks']),'exit_code':process.returncode,'out':str(out)},ensure_ascii=False))
    return 0 if report['passed'] and process.returncode==0 else 1


if __name__=='__main__':raise SystemExit(main())
