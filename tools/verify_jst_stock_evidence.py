"""Isolated stock evidence UI/EXE acceptance with frozen-vs-current source checks."""
import argparse,hashlib,json,os,shutil,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]


def fixture(out):
    import openpyxl,warehouse_catalog as wc,jst_batches as batches
    import test_jst as fixtures,work_repository as wr
    raw=out/'originals';raw.mkdir()
    def catalog(stock):
        path=raw/f'河北仓_{stock}_合成资料.xlsx';book=openpyxl.Workbook();sheet=book.active;sheet.title='商品资料'
        headers=['商品编码','分类','基本售价','出库箱规','商品状态','可用数','商品名称','商品标签']
        sheet.append(headers);sheet.append(['0001','测试',2,1,'启用',stock,'核对商品','']);book.save(path);book.close()
        return wc.import_catalog(path,'河北仓')
    f=fixtures.Service();f.setUp()
    try:
        old,current,preview_catalog=catalog(100),catalog(900),catalog(500)
        saved=batches.execute(f.path,batches.preview_new(f.path,[f.h],{'河北仓':old}))
        second=f.second_store();wc.Repository(Path(f.path).parent).save(current)
        preview=batches.preview_new(f.path,[second],{'河北仓':preview_catalog})
        shutil.copytree(Path(f.path).parent,out/'settings')
        record={'synthetic':True,'live_platform':False,'head':f.h,'second':second,'batch_id':saved['batch_id'],
                'gid':saved['generated'][0],'preview':preview,'frozen_stock':100,'current_stock':900,'preview_stock':500}
        (out/'fixture.json').write_text(wr.dumps(record),'utf-8')
    finally:f.tearDown()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--exe',type=Path);parser.add_argument('--timeout',type=float,default=150);args=parser.parse_args()
    out=args.out.resolve();out.mkdir(parents=True,exist_ok=False);fixture(out);(out/'exports').mkdir()
    (out/'synthetic.json').write_text(json.dumps({'synthetic':True,'live_platform':False,'purpose':'stock-evidence-native-acceptance'}),'utf-8')
    env=os.environ.copy()
    for name in ('PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','REPLENISHMENT_DATA_DIR','REPLENISHMENT_PORTABLE_PATHS','QT_QPA_PLATFORM'):env.pop(name,None)
    env.update(PYTHONUTF8='1',REPLENISHMENT_DATA_DIR=str(out/'settings'),LOCALAPPDATA=str(out/'localappdata'),APPDATA=str(out/'roaming'))
    if not args.exe:env['QT_QPA_PLATFORM']='offscreen'
    base=[str(args.exe.resolve())] if args.exe else [sys.executable,str(ROOT/'src/app.py')]
    report={'passed':False,'live_platform':False,'synthetic_input':True,'file_destination_adapter':True,'runs':[],
            'exe_sha256':hashlib.sha256(args.exe.read_bytes()).hexdigest() if args.exe else None}
    started=time.monotonic()
    for mode in ('exercise','reopen'):
        with (out/(mode+'-process.log')).open('w',encoding='utf-8') as log:
            p=subprocess.Popen(base+['--jst-stock-evidence-smoke',str(out),mode],cwd=out,env=env,stdout=log,stderr=log)
            try:code=p.wait(args.timeout)
            except subprocess.TimeoutExpired:
                report.update(timeout=True,pid=p.pid,message='QA process still running; not force-terminated during unknown work')
                (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');raise
        file=out/(mode+'-report.json');run=json.loads(file.read_text('utf-8')) if file.is_file() else {'passed':False,'error':'Native report missing'}
        run['exit_code']=code;report['runs'].append(run);(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
        if code or not run['passed']:raise AssertionError(run.get('error') or run)
    report.update(passed=True,elapsed_seconds=time.monotonic()-started)
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps({'passed':True,'checks':sum(len(run['checks']) for run in report['runs']),
                      'frozen_exe':all(run['frozen_exe'] for run in report['runs']),'report':str(out/'report.json')},ensure_ascii=False))


if __name__=='__main__':main()
