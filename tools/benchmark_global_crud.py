"""Alternating frozen/current workers, synthetic inputs, exact output checks."""
import argparse,copy,gc,hashlib,json,os,sqlite3,statistics,subprocess,sys,threading,time,zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
DEPS=Path('D:/优团工作文件夹/outputs/replenishment_mvp_20261029/qa/export_071/qa-deps')

def digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()
def write(p,value):p.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n','utf-8')

def worker(a):
    sys.path[:0]=[str(a.source/d) for d in ('src','tests')]+[str(DEPS)]
    import psutil,openpyxl,work_repository as wr,normal_orders as no,daily_batch as listing,result_semantics as sem
    import lifecycle_batch as batch,settings_history as history
    import test_archive_orders as fixture
    from store_parameter_import import base
    from openpyxl.worksheet._read_only import ReadOnlyWorksheet
    a.out.mkdir(exist_ok=False);process=psutil.Process();reports=[]
    def measure(name,fn,counters=None):
        gc.collect();before=process.memory_info().rss;peak=[before];stop=threading.Event()
        def sample():
            while not stop.wait(.005):peak[0]=max(peak[0],process.memory_info().rss)
        sampler=threading.Thread(target=sample);sampler.start();start=time.perf_counter()
        try:value=fn();elapsed=time.perf_counter()-start
        finally:stop.set();sampler.join()
        peak[0]=max(peak[0],process.memory_info().rss)
        reports.append({'case':name,'seconds':elapsed,'rss_before_mib':before/2**20,'peak_worker_rss_mib':peak[0]/2**20,'rss_delta_mib':(peak[0]-before)/2**20,**(counters or {})})
        return value
    for count in (400,1000):
        path=a.out/f'formula-{count}.xlsx';book=openpyxl.Workbook();s=book.active;s.append(['商品编码','门店确认数量'])
        for i in range(count):s.append([f'SKU{i:06d}','=1+2'])
        book.save(path);book.close()
        with zipfile.ZipFile(path) as z:parts=[(i,z.read(i.filename)) for i in z.infolist()]
        ns='{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
        with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:
            for i,raw in parts:
                if i.filename=='xl/worksheets/sheet1.xml':
                    xml=ET.fromstring(raw)
                    for c in xml.iter(ns+'c'):
                        if c.find(ns+'f') is not None:c.find(ns+'v').text='3'
                    raw=ET.tostring(xml,encoding='utf-8',xml_declaration=True)
                z.writestr(i,raw)
        calls={'single_row_reparses':0};original=ReadOnlyWorksheet.iter_rows
        def counted(ws,*args,**kw):
            if kw.get('min_row') is not None and kw.get('min_row')==kw.get('max_row'):calls['single_row_reparses']+=1
            return original(ws,*args,**kw)
        with patch.object(ReadOnlyWorksheet,'iter_rows',counted):value=measure(f'formula_{count}',lambda:batch.read([path],'return'),calls)
        assert not value['issues'],value['issues'];rows=[r for d in value['documents'] for r in d['scan']['rows']]
        assert len(rows)==count and all(r['quantity']=='3' for r in rows)
        reports[-1]['output_digest']=digest([(r['sku'],r['quantity'],r['amount']) for r in rows]);del value,rows
    for count,multiplier in ((40,20),(160,20),(40,200)):
        prefix=f'list_{count}'+('_400sku' if multiplier==200 else '')
        folder=a.out/f'list-{count}-{multiplier}';folder.mkdir();work,repo,payload=fixture.seed(folder);expanded=[]
        for source in payload['result']['rows']:
            for i in range(multiplier):row=copy.deepcopy(source);row['E']=source['E']+'-'+str(i);expanded.append(row)
        with closing(sqlite3.connect(work.path)) as db,db:
            db.execute('DELETE FROM normal_revisions');db.execute('DELETE FROM normal_orders')
            for n in range(count):
                p=copy.deepcopy(payload);code='S'+str(n);oid='oid-'+code;p['result'].update(store=code,store_name='合成门店'+str(n),rows=expanded,summary=sem.summarize(expanded))
                binding=no.fingerprint(p['result']);blob=no.encode(p,db)
                db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(wr.today(),code,'normal_model',oid,'BH-'+code+'-ZC',1));db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',(oid,1,binding,'2026-11-16T00:00:00+08:00',blob))
        source_hash=hashlib.sha256(work.path.read_bytes()).hexdigest();original=sqlite3.connect
        for label in ('cold_page','warm_page','warm_one_order'):
            counters={'selects':0,'parents':count,'skus_per_parent':len(expanded)}
            def traced(*args,**kw):
                db=original(*args,**kw)
                def trace(sql):
                    if sql.lstrip().upper().startswith('SELECT'):counters['selects']+=1
                db.set_trace_callback(trace);return db
            request={'from':wr.today(),'to':wr.today(),'page':0}
            if label=='warm_one_order':request['scope']=[['oid-S0','河北仓']]
            with patch.object(sqlite3,'connect',traced):data=measure(f'{prefix}_{label}',lambda:listing.read(work.path,folder/'index.db',request),counters)
            fields=('oid','warehouse','number','store','name','status','next','present','health','flow_version','visibility_token','old_revision','list_position','note')
            reports[-1]['output_digest']=digest({'rows':[{k:r.get(k) for k in fields} for r in data['records']],'count':data['count'],'counts':data['counts'],'scope':data['scope']})
        assert hashlib.sha256(work.path.read_bytes()).hexdigest()==source_hash,'Read modified business database'
    repo=wr.Repository(a.out/'history');record=base();before={f'S{i}':copy.deepcopy(record) for i in range(10)}
    with closing(repo.connect()) as db,db:
        db.execute(history.DDL)
        for n in range(500):
            after=copy.deepcopy(before)
            for value in after.values():value['ui'].update(mode=2,amount=str(n+1))
            db.execute('INSERT INTO settings_history VALUES(?,?,?,?,?)',(f'{n:032x}','2026-11-16T00:00:00+08:00','合成预算修改',wr.dumps(before),wr.dumps(after)));before=after
    def load_history():
        if hasattr(history,'page'):
            p=history.page(repo);return p['items'],history.entry(repo,p['items'][0]['id'])
        all_values=history.history(repo)
        return [dict(id=e['id'],at=e['at'],action=e['action'],stores=len(e['after'])) for e in all_values[:50]],all_values
    values=measure('history_500_initial_open',load_history)
    metadata,detail=values
    if isinstance(detail,list):detail=detail[0]
    reports[-1]['output_digest']=digest({'metadata':[{k:e[k] for k in ('id','at','action','stores')} for e in metadata],'selected':detail})
    modules=('model.py','lifecycle_io.py','lifecycle_home.py','daily_batch.py','settings_history.py','payment_matching.py')
    write(a.out/'report.json',{'runs':reports,'pid':os.getpid(),'native_peak_worker_rss_mib':process.memory_info().peak_wset/2**20,
                             'source_sha256':{name:hashlib.sha256((a.source/'src'/name).read_bytes()).hexdigest() for name in modules}})

def main():
    p=argparse.ArgumentParser();p.add_argument('--baseline',type=Path);p.add_argument('--out',type=Path,required=True);p.add_argument('--source',type=Path);p.add_argument('--worker',action='store_true');a=p.parse_args()
    if a.worker:return worker(a)
    a.out.mkdir(exist_ok=False);runs=[]
    for n in range(3):
        for label,source in ([('old',a.baseline),('new',ROOT)] if n%2==0 else [('new',ROOT),('old',a.baseline)]):
            target=a.out/f'{label}-{n}';log=a.out/f'{label}-{n}.log'
            with log.open('wb') as stream:
                r=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--worker','--source',str(source),'--out',str(target)],stdout=stream,stderr=stream,env={**os.environ,'PYTHONUTF8':'1'})
            if r.returncode:raise RuntimeError(str(log))
            runs.append({'variant':label,'round':n,**json.loads((target/'report.json').read_text('utf-8'))})
    cases={}
    for case in runs[0]['runs']:
        key=case['case'];old=[next(v for v in r['runs'] if v['case']==key) for r in runs if r['variant']=='old'];new=[next(v for v in r['runs'] if v['case']==key) for r in runs if r['variant']=='new']
        assert len({v['output_digest'] for v in old+new})==1,'Output mismatch: '+key
        cases[key]={label:{field:statistics.median(v[field] for v in values) for field in ('seconds','peak_worker_rss_mib','rss_delta_mib')} for label,values in [('old',old),('new',new)]}
        cases[key]['outputs_equal']=True
    result={'passed':True,'cases':cases,'workers':runs,'live_platform':False,'limitations':'Synthetic development-PC measurements, three alternating runs. RSS samples are actual worker processes; no business-machine capacity claim. History compares first-page metadata and exact selected full record. Cold lists still compute full-scope totals.'}
    write(a.out/'comparison.json',result);print(json.dumps({'passed':True,'cases':cases},ensure_ascii=False))

if __name__=='__main__':main()
