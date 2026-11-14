"""Run actual source or packaged UI recovery with isolated synthetic orders."""
import argparse,hashlib,json,os,shutil,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]


def fixture(out):
    import openpyxl,jst_batches as batches,work_repository as wr
    import test_jst as examples
    source=out/'fresh-catalog.xlsx';b=openpyxl.Workbook();s=b.active;s.title='商品资料'
    fields=['商品编码','分类','基本售价','出库箱规','商品状态','可用数','商品名称','商品标签']
    s.append(fields);s.append(['0001','测试',2,1,'备用',700,'核对商品','']);b.save(source);b.close()
    f=examples.Service();f.setUp()
    try:
        other=f.second_store();plan=batches.preview_new(f.path,[f.h,other],{'河北仓':examples.catalog(stock=100)})
        batches.register(f.path,plan);shutil.copytree(Path(f.path).parent,out/'settings')
        record={'head':f.h,'other':other,'batch_id':plan['payload']['batch_id'],'fresh_catalog':str(source)}
        (out/'fixture.json').write_text(wr.dumps(record),'utf-8')
    finally:f.tearDown()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);p.add_argument('--exe',type=Path);p.add_argument('--timeout',type=float,default=140);a=p.parse_args()
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False);fixture(out);(out/'exports').mkdir()
    (out/'synthetic.json').write_text(json.dumps({'synthetic':True,'live_platform':False,'purpose':'pending-allocation-recovery'}),'utf-8')
    env=os.environ.copy()
    for name in ('PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','REPLENISHMENT_DATA_DIR','REPLENISHMENT_PORTABLE_PATHS','QT_QPA_PLATFORM'):env.pop(name,None)
    env.update(PYTHONUTF8='1',REPLENISHMENT_DATA_DIR=str(out/'settings'),LOCALAPPDATA=str(out/'localappdata'),APPDATA=str(out/'roaming'))
    if not a.exe:env['QT_QPA_PLATFORM']='offscreen'
    command=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    report={'passed':False,'runs':[],'synthetic_input':True,'live_platform':False,'dialogs_not_stubbed':True,
            'exe_sha256':hashlib.sha256(a.exe.read_bytes()).hexdigest() if a.exe else None}
    started=time.monotonic()
    for mode in ('exercise','reopen'):
        with (out/(mode+'-process.log')).open('wb') as log:
            proc=subprocess.Popen(command+['--jst-batch-recovery-smoke',str(out),mode],cwd=out,env=env,stdout=log,stderr=log)
            try:code=proc.wait(a.timeout)
            except subprocess.TimeoutExpired:
                report.update(timeout=True,pid=proc.pid,error='Synthetic EXE still running; not forcibly terminated')
                (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');raise
        path=out/(mode+'-report.json');run=json.loads(path.read_text('utf-8')) if path.exists() else {'passed':False,'error':'Native report missing'}
        run['exit_code']=code;report['runs'].append(run);(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
        if code or not run['passed']:raise AssertionError(run.get('error') or run)
    report.update(passed=True,elapsed_seconds=time.monotonic()-started)
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps({'passed':True,'checks':sum(len(r['checks']) for r in report['runs']),'frozen_exe':all(r['frozen_exe'] for r in report['runs']),'report':str(out/'report.json')},ensure_ascii=False))


if __name__=='__main__':main()
