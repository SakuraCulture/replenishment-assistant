"""Fresh, synthetic two-day/two-warehouse analytics acceptance; never uses live settings."""
import argparse, hashlib, json, os, sqlite3, subprocess, sys, time
from contextlib import closing
from datetime import date,timedelta
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/p) for p in ('src','tools','tests')]


def fixture(out):
    import test_order_analytics as seed, order_lifecycle as lc, lifecycle_io as io, work_repository as wr
    import payment_references as refs, openpyxl, export_settings
    from test_analytics_identifiers import seed_purchase
    out.mkdir(parents=True,exist_ok=False); settings=out/'settings'; settings.mkdir()
    f=seed.AnalyticsTests(); f.setUp(); path=settings/'workbench.sqlite3'
    try:
        with closing(sqlite3.connect(f.path)) as source,closing(sqlite3.connect(path)) as target:source.backup(target)
    finally:f.tearDown()
    yesterday=(date.fromisoformat(wr.today())-timedelta(days=1)).isoformat()
    with closing(sqlite3.connect(path)) as db,db:
        row=db.execute('SELECT * FROM normal_orders').fetchone()
        db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(yesterday,row[1],row[2],'previous-day-oid','BH-PREVIOUS-ZC',1))
        rev=db.execute('SELECT digest,saved,payload FROM normal_revisions WHERE oid=? AND revision=?',(row[3],row[5])).fetchone()
        db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',('previous-day-oid',1,*rev))
    heads=lc.current_targets(path)
    for stage in ('return','payment'):
        book=openpyxl.Workbook();sheet=book.active;sheet.append(io.fields(stage))
        for n,h in enumerate(heads):
            sku=next(iter(h['products'])); r={'补货单号':h['number'],'签发修订':h['revision'],'门店编码':h['store'],'供货仓':h['warehouse'],
                '商品编码':sku,'商品名称':'合成商品','数量单位':'库存单位',io.QUANTITIES[stage]:5,io.DOCNUM[stage]:'JST-NATIVE-'+str(n),io.DOCDATE[stage]:h['day'],'实际金额':12,'币种':'CNY'}
            sheet.append([r.get(k) for k in io.fields(stage)])
        file=out/(stage+'.xlsx');book.save(file);book.close();info=io.inspect([file])[0];sheet=info['sheets'][0]
        scan=io.scan(stage,[{**info,**sheet,'mapping':{h:i for i,h in enumerate(sheet['headers'])}}]);lc.commit(path,lc.prepare_numbered(path,scan))
    expected=[]; component=None
    for n,h in enumerate(heads):
        internal='0009942110800'+str(n); refs.save(path,refs.read(path,h),internal)
        number=None
        if n<len(heads)-1:
            number='PO-OFFLINE-000'+str(n);payment=lc.snapshot(path,h)['documents']['payment'];component,_=seed_purchase(path,h,payment,id='purchase-'+str(n),number=number)
        expected.append({'order':h['number'],'warehouse':h['warehouse'],'jst':internal,'po':number})
    with closing(sqlite3.connect(component)) as db,db:
        db.execute('INSERT INTO purchase_receipts VALUES(?,?)',('purchase-1',json.dumps({'state':'VOIDED','summary':{'status':'已作废'}})))
    (out/'exports').mkdir();export_settings.save(settings/export_settings.FILENAME,str(out/'exports'))
    (out/'fixture.json').write_text(json.dumps({'from':yesterday,'to':wr.today(),'expected':expected,'synthetic':True,'live_platform':False},ensure_ascii=False,indent=2),'utf-8')


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',required=True,type=Path);p.add_argument('--exe',type=Path);a=p.parse_args()
    out=a.out.resolve();out.relative_to(ROOT/'qa');fixture(out)
    env={**os.environ,'PYTHONUTF8':'1','QT_QPA_PLATFORM':'windows','REPLENISHMENT_DATA_DIR':str(out/'settings'),'LOCALAPPDATA':str(out/'localappdata')}
    command=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    with (out/'process.log').open('wb') as log:
        proc=subprocess.Popen(command+['--analytics-identifiers-smoke',str(out)],cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW);started=time.monotonic()
        while proc.poll() is None:
            if time.monotonic()-started>180:raise TimeoutError(f'Isolated process {proc.pid} retained; no forced termination')
            time.sleep(.2)
    report=json.loads((out/'report.json').read_text('utf-8'));report['process_exit']=proc.returncode
    if a.exe:
        with a.exe.open('rb') as stream:report['exe_sha256']=hashlib.file_digest(stream,'sha256').hexdigest()
    (out/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');print(json.dumps(report,ensure_ascii=False))
    return 0 if report['passed'] and proc.returncode==0 else 1

if __name__=='__main__':sys.exit(main())
