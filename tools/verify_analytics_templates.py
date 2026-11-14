"""Actual UI export/reopen acceptance using newly built synthetic frozen facts."""
import argparse, hashlib, json, os, shutil, sqlite3, subprocess, sys, time
from contextlib import closing
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]


def fixture(out):
    import test_order_analytics as examples, normal_orders as orders, purchases
    import source_archive, operations_flow, order_lifecycle, export_settings, payment_references
    import openpyxl
    from test_analytics_identifiers import seed_purchase
    f=examples.AnalyticsTests();f.setUp()
    try:
        row=f.p['result']['rows'][0]
        row.update(F='0000000123456',K=4,J=60,L=0,BF=148,AP=140,AL=8,BD=24,
                   _mandatory_holding={'minimum':24})
        row['_meta']['export_updated']='2026-11-14 12:00:00';f.save_payload()
        catalog=f.root/'frozen-catalog.xlsx';book=openpyxl.Workbook();sheet=book.active;sheet.title='原资料'
        sheet.append(['商品编码','商品状态','虚拟分类','可用数','供应商名称','分类'])
        sheet.append(['0001','启用','',240,'合成水饮供应商','饮品/饮用水/纯净水']);book.save(catalog);book.close()
        digest=hashlib.sha256(catalog.read_bytes()).hexdigest();source_archive.capture(f.path,catalog,digest)
        f.catalogs['河北仓']={'source_sha256':digest,'sheet':'原资料','revision':digest,'items':{}}
        with closing(sqlite3.connect(f.path)) as db,db:
            db.execute('UPDATE normal_work_revisions SET payload=?',(orders.encode({'catalogs':f.catalogs},db),))
            operations_flow.schema(db);db.execute('INSERT INTO lifecycle_routes VALUES(?,?,2)',(f.f.h['oid'],1))
        f.f.heads=purchases.targets(f.path);f.f.h=next(h for h in f.f.heads if h['warehouse']=='河北仓')
        f.f.save('return');operations_flow.adopt(f.path,[f.f.h]);f.f.save('payment');f.f.save('receipt',[{'实际收货数量':4}])
        payment_references.save(f.path,payment_references.read(f.path,f.f.h),'000JST0001')
        seed_purchase(f.path,f.f.h,order_lifecycle.snapshot(f.path,f.f.h)['documents']['payment'],number='PO00001')
        report=f.report();shutil.copytree(f.path.parent,out/'settings')
        metadata={'day':report['query']['from'],'head':f.f.h,'expected_orders':len(report['heads']),
                  'expected_rows':len(report['rows']),'jst':'000JST0001','po':'PO00001'}
        (out/'fixture.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),'utf-8',newline='\n')
    finally:f.tearDown()
    (out/'exports').mkdir()
    export_settings.save(out/'settings'/export_settings.FILENAME,str(out/'exports'))
    (out/'synthetic.json').write_text(json.dumps({'synthetic':True,'live_platform':False,'purpose':'analytics-template-export'}),'utf-8',newline='\n')


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--exe',type=Path);p.add_argument('--timeout',type=float,default=150);a=p.parse_args()
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False);fixture(out)
    env=os.environ.copy()
    for k in ('PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','REPLENISHMENT_DATA_DIR','REPLENISHMENT_PORTABLE_PATHS','QT_QPA_PLATFORM'):env.pop(k,None)
    env.update(PYTHONUTF8='1',REPLENISHMENT_DATA_DIR=str(out/'settings'),LOCALAPPDATA=str(out/'localappdata'),APPDATA=str(out/'roaming'))
    if not a.exe:env['QT_QPA_PLATFORM']='offscreen'
    command=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    result={'passed':False,'runs':[],'live_platform':False,'synthetic_input':True,
            'exe_sha256':hashlib.sha256(a.exe.read_bytes()).hexdigest() if a.exe else None};start=time.monotonic()
    for mode in ('exercise','reopen'):
        with (out/(mode+'-process.log')).open('wb') as log:
            process=subprocess.Popen(command+['--analytics-templates-smoke',str(out),mode],cwd=out,env=env,stdout=log,stderr=log)
            try:code=process.wait(a.timeout)
            except subprocess.TimeoutExpired:
                result.update(error='Synthetic application timed out; not forcibly terminated',pid=process.pid)
                (out/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8',newline='\n');raise
        report=out/(mode+'-report.json');run=json.loads(report.read_text('utf-8')) if report.exists() else {'passed':False,'error':'Native report missing'}
        run['exit_code']=code;result['runs'].append(run)
        (out/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8',newline='\n')
        if code or not run['passed']:raise AssertionError(run)
    result.update(passed=True,elapsed_seconds=time.monotonic()-start)
    (out/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8',newline='\n')
    print(json.dumps({'passed':True,'checks':sum(len(r['checks']) for r in result['runs']),'report':str(out/'report.json')},ensure_ascii=False))


if __name__=='__main__':main()
