"""Repeatable Qt/source or EXE acceptance with independent synthetic downloaded-task fixtures."""
import argparse,json,os,subprocess,sys,uuid,hashlib,time
from pathlib import Path
import openpyxl
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
import warehouse_catalog as wc
from catalog_sync_client import Client
p=argparse.ArgumentParser();p.add_argument('--output',required=True,type=Path);p.add_argument('--exe',type=Path);a=p.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);settings=out/'user';service=settings/'jst_catalog_sync';c=Client(service);c.start();c.close()
root=service/'downloads/batch/item-sd';root.mkdir(parents=True);f=root/'synthetic.xlsx';b=openpyxl.Workbook();b.active.append(wc.REQUIRED+['商品名称']);b.active.append(['0001','快销',2,24,'启用',240,'合成品一']);b.active.append(['123456789012345678','快销',None,None,'禁用',0,'合成品二']);b.save(f);b.close();sha=hashlib.sha256(f.read_bytes()).hexdigest()
accounts={};items=[]
for n,wh in [('sd','山东仓'),('hb','河北仓')]:
 account={'id':n,'warehouse':wh,'expectedCompany':'SYNTHETIC QA '+n,'expectedCoid':'1' if n=='sd' else '3','expectedUid':'2' if n=='sd' else '4','mappingVerified':True,'bindingVersion':1,'binding':{'actualProfilePath':str(out/'nonexistent'/n/'Default')},'enabled':True};accounts[wh]=account
 frozen={k:v for k,v in account.items() if k!='enabled'};native={'stage':'DOWNLOADED','submissions':1,'file':str(f),'sha256':sha,'nativeTaskRef':'123','scope':{'expectedRows':2,'coid':'1','uid':'2'}} if n=='sd' else None
 items.append({'id':'item-'+n,'index':len(items),'account':frozen,'expected_catalog_digest':None,'stage':'DOWNLOADED' if native else 'QUEUED','native':native,'receipt':None,'adoption':None,'observation':None,'error':None})
jobs=service/'jobs.json';data=json.loads(jobs.read_text('utf-8'));data['accounts']=accounts;data['batches']={'batch':{'id':'batch','purpose':'default_catalog','createdAt':'2026-11-03T00:00:00Z','state':'NEEDS_ATTENTION','items':items}};jobs.write_text(json.dumps(data,ensure_ascii=False),'utf-8')
env=os.environ.copy();env['REPLENISHMENT_DATA_DIR']=str(out/'user');env['PYTHONUTF8']='1'
cmd=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
with (out/'process.log').open('w',encoding='utf-8') as log:
 r=subprocess.run(cmd+['--catalog-sync-smoke',str(out)],env=env,stdout=log,stderr=log,timeout=180)
report=json.loads((out/'report.json').read_text('utf-8'));report.update(process_exit=r.returncode,exe_sha256=hashlib.sha256(a.exe.read_bytes()).hexdigest() if a.exe else None);(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');print(json.dumps(report,ensure_ascii=False))
if r.returncode or not report['passed']:raise SystemExit(1)
