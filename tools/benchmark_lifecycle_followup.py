"""Isolated natural-payment and local AX preparation, identical facts and RSS."""
import argparse,gc,gzip,hashlib,json,os,shutil,sqlite3,statistics,subprocess,sys,threading,time,uuid
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
CORE=('jst_orders.py','jst_payment.py','jst_results.py','order_lifecycle.py','lifecycle_read_proofs.py','aoxiang_purchase.py','aoxiang_payment_preflight.py','warehouse_execution.py','normal_orders.py')
def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n','utf-8')
def clone(source,target):
    with closing(sqlite3.connect(source)) as src,closing(sqlite3.connect(target)) as dst:src.backup(dst)
def worker(a):
    sys.path[:0]=[str(a.source/'src'),str(a.deps)]
    import psutil,work_repository as wr,order_lifecycle as lc,jst_orders as jo,jst_batches as jb,jst_results as jr,jst_payment as jp
    import aoxiang_purchase as ax,warehouse_catalog as wc
    from unittest.mock import patch
    assert json.loads((a.fixture/'synthetic.json').read_text('utf-8'))['synthetic']
    a.out.mkdir(parents=True,exist_ok=False);folder=a.out/'isolated-db';folder.mkdir();db=folder/'workbench.sqlite3';clone(a.fixture/'complete.sqlite3',db)
    heads=lc.current_targets(db);assert len(heads)==6
    items={s:{'商品编码':s,'分类':'合成分类','基本售价':2,'出库箱规':1,'商品状态':'启用','可用数':100000,'商品名称':p['name'],'商品标签':'','_row':i+2}
           for i,(s,p) in enumerate({s:p for h in heads for s,p in h['products'].items()}.items())}
    cat={'schema':1,'warehouse':'河北仓','items':items,'revision':wc.revision(items),'imported_at':'2026-11-15T12:00:00+08:00','source_path':str(a.fixture/'synthetic-catalog-not-a-file.xlsx'),'source_sha256':'synthetic-only','sheet':'合成资料'}
    config={'configured':True,'enabled':True,'account_binding':{'account_id':'SYN-A','merchant_code':'SYN-M'},'store_mapping_version':'synthetic-v1',
            'targets':[{'buyerWarehouseCode':h['store'],'displayName':h['name']} for h in heads]};suppliers={'河北仓':'SYN-HB','山东仓':'SYN-SD'}
    counter=[0]
    def uid():counter[0]+=1;return uuid.UUID(int=counter[0])
    def business(v):
        if isinstance(v,dict):return {k:business(x) for k,x in v.items() if k!='_read_proofs'}
        if isinstance(v,list):return [business(x) for x in v]
        return v
    def sig(v):return hashlib.sha256(wr.dumps(v).encode()).hexdigest()
    def facts():
        out={}
        with closing(sqlite3.connect(db)) as c:
            assert c.execute('PRAGMA quick_check').fetchone()[0]=='ok'
            for name,definition in c.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
                h=hashlib.sha256();n=0
                for row in c.execute('SELECT * FROM "'+name+'" ORDER BY rowid'):
                    h.update(wr.dumps([{'blob':v.hex()} if isinstance(v,bytes) else v for v in row]).encode());h.update(b'\n');n+=1
                out[name]={'rows':n,'sha256':h.hexdigest(),'definition':definition}
        return out
    phases=[];process=psutil.Process();hashes={n:sha(a.source/'src'/n) for n in CORE}
    def measure(name,fn):
        gc.collect();peak=[process.memory_info().rss];stop=threading.Event();progress=[]
        def sample():
            while not stop.wait(.02):peak[0]=max(peak[0],process.memory_info().rss)
        t=threading.Thread(target=sample);t.start();io=process.io_counters();start=time.perf_counter()
        try:value=fn(progress.append);elapsed=time.perf_counter()-start;after=process.io_counters()
        finally:stop.set();t.join()
        peak[0]=max(peak[0],process.memory_info().rss)
        item={'phase':name,'seconds':elapsed,'peak_rss_mib':peak[0]/2**20,'process_read_mib':(after.read_bytes-io.read_bytes)/2**20,
              'process_write_mib':(after.write_bytes-io.write_bytes)/2**20,'result_sha256':sig(business(value)),'ordered_tables':facts()}
        # Proof construction does not emit business progress. Capture exact text too.
        item['progress_sha256']=sig([str(x) for x in progress]);phases.append(item)
        print(json.dumps({k:item[k] for k in ('phase','seconds','peak_rss_mib')},ensure_ascii=False),flush=True)
        if a.trial==1:
            with gzip.open(a.out/(name+'-business.json.gz'),'wt',encoding='utf-8') as f:f.write(wr.dumps(business(value)))
        return value
    with patch('uuid.uuid4',uid),patch('work_repository.now',lambda:'2026-11-15T12:00:00+08:00'):
        generated=jo.commit(db,jb.register(db,jb.preview_new(db,heads,{'河北仓':cat})))
        assert not generated['errors'] and len(generated['generated'])==6
        entries=jr.prepare_export_ids(db,generated['generated'])['entries']
        plan=measure('natural_prepare',lambda progress:jp.prepare(db,entries,progress));assert len(plan['plans'])==6
        measure('natural_save',lambda progress:lc.commit(db,plan,progress));del plan;gc.collect()
        plans=measure('purchase_freeze',lambda progress:[ax.freeze_payment(db,h,config,suppliers) for h in heads])
        measure('purchase_validate',lambda progress:[ax.validate_basis(db,p,config,suppliers) for p in plans])
        assert len(plans)==6 and all(p['rows'] and not p['create_external_order'] for p in plans)
    assert hashes=={n:sha(a.source/'src'/n) for n in CORE}
    write(a.out/'report.json',{'passed':True,'variant':a.variant,'trial':a.trial,'core_source_sha256':hashes,'phases':phases})
    assert folder.resolve().is_relative_to(a.out.resolve()) and folder.name=='isolated-db';shutil.rmtree(folder)
def main():
    p=argparse.ArgumentParser();p.add_argument('--fixture',type=Path,required=True);p.add_argument('--out',type=Path,required=True);p.add_argument('--deps',type=Path,required=True)
    p.add_argument('--baseline',type=Path);p.add_argument('--source',type=Path);p.add_argument('--worker',action='store_true');p.add_argument('--variant');p.add_argument('--trial',type=int,default=1);p.add_argument('--repeats',type=int,default=3);a=p.parse_args()
    if a.worker:return worker(a)
    a.out.mkdir(parents=True,exist_ok=False);inputs={p.name:sha(p) for p in a.fixture.iterdir() if p.is_file()};reports=[]
    for trial in range(1,a.repeats+1):
        for variant in (('old','new') if trial%2 else ('new','old')):
            target=a.out/f'{trial}-{variant}';source=a.baseline.resolve() if variant=='old' else ROOT
            command=[sys.executable,str(Path(__file__).resolve()),'--worker','--fixture',str(a.fixture.resolve()),'--out',str(target.resolve()),'--deps',str(a.deps.resolve()),'--source',str(source),'--variant',variant,'--trial',str(trial)]
            with (a.out/f'{trial}-{variant}.log').open('wb') as log:r=subprocess.run(command,stdout=log,stderr=log,env={**os.environ,'PYTHONUTF8':'1'})
            if r.returncode:raise RuntimeError('Failed worker; retained '+str(log.name))
            reports.append(json.loads((target/'report.json').read_text('utf-8')));print('completed',trial,variant,flush=True)
    expected=None
    for r in reports:
        current=[{k:v for k,v in ph.items() if k not in ('seconds','peak_rss_mib','process_read_mib','process_write_mib')} for ph in r['phases']]
        if expected is None:expected=current
        assert current==expected,('Ordered business facts differ',r['variant'],r['trial'])
    summary={}
    for phase in [x['phase'] for x in reports[0]['phases']]:
        summary[phase]={variant:{k:statistics.median(next(ph for ph in r['phases'] if ph['phase']==phase)[k] for r in reports if r['variant']==variant)
                                for k in ('seconds','peak_rss_mib','process_read_mib','process_write_mib')} for variant in ('old','new')}
    assert inputs=={p.name:sha(p) for p in a.fixture.iterdir() if p.is_file()}
    write(a.out/'report.json',{'passed':True,'synthetic':True,'live_platform':False,'orders':6,'rows':12000,'repeats':a.repeats,
      'business_equivalence':{'plans':True,'ordered_tables':True,'progress':True},'summary':summary,'runs':reports,'input_sha256':inputs,
      'notes':'Alternating independent processes. Windows process IO includes cache. RSS sampled every 20ms, worker only. DB copy, file parsing and generation fixture outside timing. No platform, no business files.'})
    print(json.dumps({'passed':True,'summary':summary},ensure_ascii=False),flush=True)
if __name__=='__main__':main()
