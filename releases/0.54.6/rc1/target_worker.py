"""20k-row probes using real templates and real lifecycle preparation."""
import argparse,ast,copy,gc,hashlib,json,os,sqlite3,sys,threading,time
from contextlib import closing
from datetime import datetime
from decimal import Decimal
from pathlib import Path
Q=Path(__file__).resolve().parent;OUT=Q.parents[1]
def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n','utf-8')
def main(a):
 sys.path[:0]=[str(a.source/'src')];sys.path.append(str(Q.parent/'export_071/qa-deps'))
 import psutil,work_repository as wr,openpyxl
 a.output.mkdir(parents=True,exist_ok=False);process=psutil.Process();metrics=[]
 def measure(name,fn):
  stop=threading.Event();peak=[0];rss=[0]
  def sample():
   m=process.memory_info();peak[0]=max(peak[0],getattr(m,'private',m.rss));rss[0]=max(rss[0],m.rss)
  def watch():
   while not stop.wait(.02):sample()
  sample();t=threading.Thread(target=watch,daemon=True);t.start();io=process.io_counters();start=time.perf_counter();cpu=time.process_time()
  try:value=fn()
  finally:
   elapsed=time.perf_counter()-start;cpu=time.process_time()-cpu;after=process.io_counters();stop.set();t.join();sample()
  item={'phase':name,'seconds':elapsed,'cpu_seconds':cpu,'peak_private_mib':peak[0]/2**20,'peak_rss_mib':rss[0]/2**20,
        'process_read_mib':(after.read_bytes-io.read_bytes)/2**20,'process_write_mib':(after.write_bytes-io.write_bytes)/2**20}
  metrics.append(item);print(json.dumps(item),flush=True);return value
 report={'passed':False,'source':str(a.source),'kind':a.kind,'rows':a.rows,'metrics':metrics,'synthetic':True,'live_platform':False}
 try:
  if a.kind=='template':
   import analytics_template_export as layout
   from analytics_formula_cache import FormulaCache
   from xlsx_exports import stream_book
   factory_path=OUT/'build_0.54.5_rc1/source/tests/test_analytics_template_export.py'
   tree=ast.parse(factory_path.read_text('utf-8'));fact=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='fact')
   namespace={'Decimal':Decimal,'export':layout};exec(compile(ast.Module(body=[fact],type_ignores=[]),str(factory_path),'exec'),namespace)
   rows=[]
   for i in range(a.rows):
    r=namespace['fact'](sku=f'5{i:012d}',group='必采' if i%3==0 else '非必采',price=None if i%97==0 else '2.5')
    r['name']='合成商品'+str(i);r['template_attributes']['stock']=Decimal(-1) if i%53==0 else Decimal(i%19)
    rows.append(r)
   path=a.output/'20k-SKU完整明细.xlsx'
   def exported():
    with FormulaCache(a.output) as cache,stream_book() as book:
     book._analytics_formula_cache=cache.add;book.properties.created=datetime(2026,11,16,12)
     layout.append_detail(book,{},rows);book.save(path);cache.apply(path)
   measure('template_detail_20k',exported)
   report['file']=str(path)
  else:
   import order_lifecycle as lc,lifecycle_batch as batch,lifecycle_io as io
   wr.today=lambda:'2026-11-16';wr.now=lambda:'2026-11-16T12:00:00+08:00'
   seed=OUT/'qa/full_flow_performance_0545_20261116_01/sign-export-17x20k/new/settings/workbench.sqlite3'
   path=a.output/'workbench.sqlite3'
   with closing(sqlite3.connect(seed.as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(path)) as dst:src.backup(dst)
   h=next(h for h in lc.current_targets(path) if len(h['products'])==a.rows)
   file=a.output/'20k门店回传.xlsx';book=openpyxl.Workbook(write_only=True);ws=book.create_sheet('门店回传单');fields=io.fields('return');ws.append(fields)
   for sku,p in h['products'].items():
    r={'补货单号':h['number'],'签发修订':h['revision'],'门店编码':h['store'],'供货仓':h['warehouse'],'商品编码':sku,
       '商品名称':p['name'],'数量单位':'库存单位','门店确认数量':p['quantity'],'回传单号':'SYN-20K','回传日期':wr.today()}
    ws.append([r.get(k) for k in fields])
   book.save(file);book.close();documents=batch.read([file],'return')['documents']
   plan=measure('lifecycle_prepare_20k',lambda:batch.prepare(path,documents,[h]))
   # Count version observations separately so tracing does not bias timings.
   statements=[];original=sqlite3.connect
   def observed(*args,**kwargs):
    db=original(*args,**kwargs);db.set_trace_callback(statements.append);return db
   from unittest.mock import patch
   with patch.object(sqlite3,'connect',observed):again=batch.prepare(path,documents,[h])
   assert plan['plans'][0]['payload']==again['plans'][0]['payload']
   report['data_version_calls']=sum(s=='PRAGMA data_version' for s in statements);report['sql_statements']=len(statements)
   report['facts_sha256']=hashlib.sha256(wr.dumps([[r['sku'],r['base_quantity'],r['quantity'],r['amount']] for r in plan['plans'][0]['payload']['rows']]).encode()).hexdigest()
   report['payload_rows']=len(plan['plans'][0]['payload']['rows']);assert report['payload_rows']==a.rows
   receipt=measure('lifecycle_save_20k',lambda:lc.commit(path,plan));assert len(receipt)==1
   report['receipt']=[{k:r[k] for k in ('number','stage','version','changed')} for r in receipt]
  report['passed']=True
 except BaseException:
  import traceback
  report['error']=traceback.format_exc()
 write(a.output/'result.json',report);return 0 if report['passed'] else 1
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
 p.add_argument('--kind',choices=('template','lifecycle'),required=True);p.add_argument('--rows',type=int,default=20000)
 raise SystemExit(main(p.parse_args()))
