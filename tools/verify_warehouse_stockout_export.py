"""Fresh isolated native warehouse export acceptance; no real accounts or data."""
import argparse,hashlib,json,os,subprocess,sys,threading,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/p) for p in ('src','tests')]


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',required=True,type=Path);p.add_argument('--exe',type=Path);args=p.parse_args()
    out=args.out.resolve();out.relative_to(ROOT/'qa');out.mkdir(parents=True,exist_ok=False);(out/'inputs').mkdir();(out/'exports').mkdir()
    import warehouse_stockout as service,export_settings
    from warehouse_stockout_worker import execute
    from test_warehouse_stockout import workbook,ROWS
    from stockout_common import today
    settings=out/'settings';repo=service.Repository(settings);repo.initialize()
    rows=[[f'{n:08d}','测试商品'+str(n),0 if n%2==0 else 5,'启用','','','饮料','000S1','测试供货一'] for n in range(601)]
    rows+=ROWS[3:6]+[['unknown','未知库存',None,'启用','','','食品','S2','供货二']]
    def rpc(*a,**kw):raise AssertionError('No platform calls')
    for wh,data in [('山东仓',rows),('河北仓',ROWS)]:
        path=workbook(out/'inputs'/(wh+'.xlsx'),data);execute(repo,'ingest',{'path':str(path),'warehouse':wh},threading.Event(),lambda _:None,rpc)
    export_settings.save(settings/export_settings.FILENAME,str(out/'exports'))
    (out/'synthetic.json').write_text(json.dumps({'synthetic':True,'live_platform':False}),encoding='utf-8')
    (out/'fixture.json').write_text(json.dumps({'day':today()},ensure_ascii=False),encoding='utf-8')
    command=[str(args.exe.resolve())] if args.exe else [sys.executable,str(ROOT/'src/app.py')]
    env={**os.environ,'REPLENISHMENT_DATA_DIR':str(settings),'LOCALAPPDATA':str(out/'localappdata'),'QT_QPA_PLATFORM':'windows','PYTHONUTF8':'1'}
    with (out/'process.log').open('wb') as log:
        process=subprocess.Popen(command+['--warehouse-stockout-export-smoke',str(out)],cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW);start=time.monotonic()
        while process.poll() is None:
            if time.monotonic()-start>180:raise TimeoutError(f'Owned QA process {process.pid} retained; no forced termination')
            time.sleep(.2)
    report=json.loads((out/'report.json').read_text('utf-8'));report['process_exit']=process.returncode
    if args.exe:report['exe_sha256']=hashlib.sha256(args.exe.read_bytes()).hexdigest()
    (out/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'checks':len(report['checks']),'exit_code':process.returncode,'out':str(out)},ensure_ascii=False))
    return 0 if report['passed'] and process.returncode==0 else 1


if __name__=='__main__':raise SystemExit(main())
