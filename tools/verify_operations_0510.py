"""Fresh, marked synthetic inputs for source/frozen 0.51.0 acceptance."""
import argparse,json,os,shutil,sqlite3,subprocess,sys,time
from pathlib import Path
from contextlib import closing
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]

def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2),'utf-8')

def fixture(out):
    import test_archive_orders as archive
    import test_stockout_analysis as store
    import test_warehouse_stockout as warehouse
    import store_parameter_import as spi
    import work_repository as wr
    out.mkdir(parents=True,exist_ok=False);history=out/'history-inputs';history.mkdir();work,_,_=archive.seed(history)
    settings=out/'settings';settings.mkdir();shutil.copyfile(work.path,settings/'workbench.sqlite3');work=wr.Repository(settings)
    record=spi.base();record['name']='隔离参数门店';record['ui']['primary']='山东仓';work.save_stores({'T':record})
    f=store.AnalysisTests();f.setUp()
    try:
        f.publish([('normal',30,9),('negative',30,-7),('zero',0,1)])
        folder=settings/'analysis'/'stockout';shutil.copytree(f.repo.folder,folder)
        with closing(sqlite3.connect(folder/'stockout.sqlite3')) as db,db:
            for sha, in db.execute('SELECT sha FROM originals').fetchall():db.execute('UPDATE originals SET path=? WHERE sha=?',(str(folder/'originals'/(sha+'.xlsx')),sha))
    finally:f.doCleanups()
    inputs=out/'inputs';inputs.mkdir()
    rows=[r+[2,1] for r in warehouse.ROWS]
    file=warehouse.workbook(inputs/'warehouse.xlsx',rows,warehouse.HEADERS+['基本售价','出库箱规'])
    write(out/'cases.json',{'warehouse':str(file),'expected_rate':.4,'store_day':'2026-11-07'})
    write(out/'synthetic.json',{'synthetic':True,'live_platform':False})

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',required=True);p.add_argument('--exe');a=p.parse_args();out=Path(a.out).resolve();fixture(out)
    command=[str(Path(a.exe).resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    args=command+['--operations-0510-smoke',str(out)];env={**os.environ,'REPLENISHMENT_DATA_DIR':str(out/'settings'),'PYTHONUTF8':'1'};env.pop('QT_QPA_PLATFORM',None)
    log=(out/'stdout.log').open('w',encoding='utf-8');process=subprocess.Popen(args,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
    write(out/'launcher.json',{'pid':process.pid})
    try:
        deadline=time.monotonic()+60
        while not (out/'single-ready.json').exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.15)
        if not (out/'single-ready.json').exists():raise RuntimeError('主界面未进入隔离验收，详见 stdout.log')
        second=subprocess.run(args,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=25)
        write(out/'second-launch.json',{'exit':second.returncode});process.wait(timeout=300)
        report=json.loads((out/'report.json').read_text('utf-8'));print(json.dumps({'out':str(out),'exit':process.returncode,'passed':report['passed'],'checks':len(report.get('checks',[]))},ensure_ascii=False))
        if process.returncode or second.returncode or not report['passed']:raise SystemExit(1)
    finally:
        log.close()
        # No forced termination: the marked GUI has its own bounded timeout.
        if process.poll() is None:print('隔离验收进程仍在安全收尾，PID='+str(process.pid))

if __name__=='__main__':main()
