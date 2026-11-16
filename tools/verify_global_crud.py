"""Opt-in actual GUI/EXE acceptance, synthetic data and normal close/reopen."""
import argparse,copy,hashlib,json,os,sqlite3,subprocess,sys,time
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/p) for p in ('src','tests','tools')]

def fixture(out):
    import openpyxl,normal_exports,settings_history,work_repository as wr
    from merge_fixture import Order
    from store_parameter_import import base
    from aoxiang_directory import Directory,read_directory
    from stockout_repository import Repository
    out.mkdir(exist_ok=False);o=Order(out/'synthetic','GLOBAL');original=out/'原回传.xlsx';normal_exports.export(o.signed(),original,warehouse='河北仓')
    w=openpyxl.load_workbook(original)
    for s in w:
        if s.title=='系统关联信息':continue
        col=s.max_column+1;s.cell(1,col,'自行验算')
        for n in range(2,s.max_row+3):s.cell(n,col,'=1+2')
    w.save(out/'附加验算列.xlsx');w.close()
    source=out/'门店目录.xlsx';w=openpyxl.Workbook();w.active.append(['门店编码','门店名称']);w.active.append(['NEWONLY','只在目录里的新门店']);w.save(source);w.close();Directory(o.root/'aoxiang').save(read_directory(source))
    repo=Repository(o.root);repo.initialize();repo.save_settings({'stores':[{'code':'NEWONLY','name':'目录门店'}],'routes':{'NEWONLY':{'primary':'山东仓','secondary':None}},'profile':None})
    before={'H':base()}
    with closing(o.work.connect()) as db,db:
        for n in range(65):
            after=copy.deepcopy(before);after['H']['ui'].update(mode=2,amount=str(n+1));settings_history.record(db,before,after,'隔离历史验收');before=after
    data={'path':str(o.path),'root':str(o.root),'head':o.h,'synthetic':True,'live_platform':False}
    import payment_shop_names as names
    shared='跨企业同名验收店'
    names.save(o.path,names.prepare(names.load(o.path),[{'store':o.h['store'],'warehouse':wh,'value':shared} for wh in ('河北仓','山东仓')]))
    accounts={'accounts':{key:{'warehouse':wh,'expectedCoid':coid,'expectedUid':coid,'expectedCompany':'隔离企业'+coid,'mappingVerified':True,'bindingVersion':1} for key,wh,coid in (('hb','河北仓','100'),('sd','山东仓','200'))}}
    folder=o.root/'jst_catalog_sync';folder.mkdir(exist_ok=True);(folder/'jobs.json').write_text(json.dumps(accounts,ensure_ascii=False),'utf-8')
    for wh,name,internal in (('河北仓','企业甲付款','COMPANY-A'),('山东仓','企业乙付款','COMPANY-B')):
        sku=next(r['E'] for r in o.payload['result']['rows'] if r.get('AU')==wh)
        w=openpyxl.Workbook();w.active.append(['内部订单号','店铺名称','商品编码','数量','商品金额']);w.active.append([internal,shared,sku,3,6]);w.save(out/(name+'.xlsx'));w.close()
    (out/'fixture.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),'utf-8');return data

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--exe',type=Path);a=p.parse_args();out=a.out.resolve()
    if out.exists():raise FileExistsError('Acceptance needs a new isolated directory')
    data=fixture(out);env={**os.environ,'REPLENISHMENT_DATA_DIR':data['root'],'LOCALAPPDATA':str(out/'localappdata'),'PYTHONUTF8':'1','QT_QPA_PLATFORM':'windows'}
    cmd=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')];reports=[]
    for phase in ('exercise','reopen'):
        with (out/f'process-{phase}.log').open('wb') as log:
            proc=subprocess.Popen(cmd+['--global-crud-smoke',str(out),phase],cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW);started=time.monotonic()
            while proc.poll() is None:
                if time.monotonic()-started>180:raise TimeoutError(f'Process {proc.pid} retained; no forced termination. Inspect {out}')
                time.sleep(.2)
        path=out/f'report-{phase}.json'
        if not path.exists():raise RuntimeError('Application did not reach isolated acceptance. Check normal instance protection and process log: '+str(out))
        report=json.loads(path.read_text('utf-8'));report['exit_code']=proc.returncode;reports.append(report)
        if not report['passed'] or proc.returncode:break
    result={'passed':len(reports)==2 and all(r['passed'] and r['exit_code']==0 for r in reports),'reports':reports,'live_platform':False}
    if a.exe:result['exe_sha256']=hashlib.sha256(a.exe.read_bytes()).hexdigest()
    (out/'acceptance.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8');print(json.dumps(result,ensure_ascii=False));return 0 if result['passed'] else 1

if __name__=='__main__':sys.exit(main())
