"""Generate synthetic inputs and run the actual Qt/spawn/optional packaged EXE path."""
import argparse,copy,hashlib,json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]


def fixture():
    from test_preissue import row,result
    import engine,work_repository as wr
    from version_info import MODEL_VERSION
    results={};states={}
    for i in range(11):
        code=f'QA{i+1:02d}';rows=[]
        for j,wh in enumerate(('河北仓','山东仓') if i<9 else ('山东仓',)):
            item=row(f'QA{i+1:02d}-{j+1}',q=6,step=1,price=1.25)
            item.update(A='合成门店'+code,B=code,C='QA-SUPPLIER',AU=wh,F='QA-BARCODE',AT='正常',AV='普通')
            rows.append(item)
        r=result(rows);r.update(store=code,store_name='合成门店'+code,config=engine.config(),warehouses=engine.DEFAULT_WAREHOUSES,enabled=True)
        results[code]=r;states[code]={'name':r['store_name'],'state':'已完成','error':None}
    return {'schema':1,'day':wr.today(),'model_version':MODEL_VERSION,'results':results,'states':states,
            'new_focus':None,'input_dirty':False,'admission_required':False,'lifecycle_flow':2,'loaded':None,
            'catalogs':{},'profile':None,'store_presets':{},'overrides':{},'default_by_store':{},'sales':[],
            'products':None,'legacy':True,'concurrency':2,'exception':False,'store_ui':{}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True);parser.add_argument('--exe',type=Path)
    parser.add_argument('--timeout',type=float,default=180);args=parser.parse_args()
    out=args.out.resolve();out.mkdir(parents=True,exist_ok=False)
    (out/'synthetic.json').write_text(json.dumps({'synthetic':True,'live_platform':False},indent=2),'utf-8')
    (out/'payload.json').write_text(json.dumps(fixture(),ensure_ascii=False,indent=2),'utf-8')
    env=os.environ.copy()
    for key in ('PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','REPLENISHMENT_PORTABLE_PATHS','REPLENISHMENT_DATA_DIR','QT_QPA_PLATFORM'):env.pop(key,None)
    env['PYTHONUTF8']='1';env['REPLENISHMENT_DATA_DIR']=str(out/'settings')
    env['LOCALAPPDATA']=str(out/'localappdata');env['APPDATA']=str(out/'roaming')
    if not args.exe:env['QT_QPA_PLATFORM']='offscreen'
    command=[str(args.exe.resolve())] if args.exe else [sys.executable,str(ROOT/'src/app.py')]
    command+=['--signing-feedback-smoke',str(out/'payload.json'),str(out)]
    started=time.monotonic()
    with (out/'process.log').open('w',encoding='utf-8') as log:
        process=subprocess.Popen(command,cwd=out,env=env,stdout=log,stderr=log)
        try:code=process.wait(args.timeout)
        except subprocess.TimeoutExpired:
            report={'passed':False,'timeout':True,'pid':process.pid,'message':'Isolated QA process still running; not force-terminated during an unknown commit phase'}
            (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');raise
    report=json.loads((out/'ui-report.json').read_text('utf-8'))
    report.update(exit_code=code,elapsed_seconds=time.monotonic()-started,
                  exe_sha256=hashlib.sha256(args.exe.read_bytes()).hexdigest() if args.exe else None)
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    if code or not report['passed']:raise AssertionError(report.get('error') or report)
    print(json.dumps({'passed':True,'checks':len(report['checks']),'frozen_exe':report['frozen_exe'],'report':str(out/'report.json')},ensure_ascii=False))


if __name__=='__main__':main()
