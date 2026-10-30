"""Synthetic scale probe for new services, 3 sequential runs, isolated SQLite."""
import argparse,copy,hashlib,json,shutil,sqlite3,statistics,sys,threading,time
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests'),str(ROOT/'qa/export_071/qa-deps')]
import psutil,openpyxl
import normal_orders as no,work_repository as wr,result_semantics as sem,purchases as p,purchase_io as io,followup
from test_archive_orders import seed

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=False);work,repo,base=seed(out)
    with closing(sqlite3.connect(work.path)) as db,db:
        db.execute('DELETE FROM normal_orders');db.execute('DELETE FROM normal_revisions')
        for n in range(500):
            store=f'T{n:04d}';payload=copy.deepcopy(base);result=payload['result'];result.update(store=store,store_name='合成规模店'+store);result['rows']=[]
            for j in range(100):
                row=copy.deepcopy(base['result']['rows'][j%2]);row.update(E=f'{j:05d}',D='合成商品'+str(j));result['rows'].append(row)
            result['summary']=sem.summarize(result['rows']);digest=no.fingerprint(result);db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(wr.today(),store,'normal_model','oid'+store,'BH-'+store+'-ZC',1));db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',('oid'+store,1,digest,wr.now(),no.encode(payload)))
    source=out/'synthetic-purchase.xlsx';book=openpyxl.Workbook(write_only=True);sheet=book.create_sheet('采购明细');sheet.append(io.FIELDS)
    for n in range(50):
        store=f'T{n:04d}'
        for j in range(100):
            wh='河北仓' if j%2==0 else '山东仓';suffix='HB' if j%2==0 else 'SD';r={'来源系统':'合成QA','采购订单号':'P'+store+suffix,'采购明细号':f'L{j:05d}','下单日期':wr.today(),'门店编码':store,'供货仓':wh,'商品编码':f'{j:05d}','商品名称':'合成商品'+str(j),'本版实际下单数量':5,'数量单位':'库存单位','补货单号':'BH-'+store+'-ZC-'+suffix,'签发修订':1};sheet.append([r.get(f) for f in io.FIELDS])
    book.save(source);book.close();runs=[];process=psutil.Process()
    for i in range(3):
        folder=out/f'run{i+1}';folder.mkdir();dbpath=folder/'workbench.sqlite3';shutil.copy2(work.path,dbpath);times={};peak=[process.memory_info().rss];stop=threading.Event()
        def sample():
            while not stop.wait(.02):peak[0]=max(peak[0],process.memory_info().rss)
        thread=threading.Thread(target=sample);thread.start()
        def timed(name,fn):
            start=time.perf_counter();value=fn();times[name]=time.perf_counter()-start;return value
        try:
            home=timed('home_cold_1000_warehouse_orders',lambda:followup.read(dbpath,folder/'index.db',{}));timed('home_warm_same_scope',lambda:followup.read(dbpath,folder/'index.db',{}))
            def read():
                info=io.inspect([source])[0];return io.scan([{**info,**info['sheets'][0],'mapping':{f:j for j,f in enumerate(io.FIELDS)}}])
            scan=timed('inspect_and_read_5000_purchase_lines',read);scopes=list(dict.fromkeys((r['store'],r['warehouse']) for r in scan['rows']));heads=timed('load_100_targets',lambda:p.targets(dbpath,scopes));scan=p.auto_links(scan,heads);plan=timed('prepare_100_purchase_orders',lambda:p.prepare(dbpath,scan));receipt=timed('commit_100_orders_including_backup',lambda:p.commit(dbpath,plan));timed('home_after_purchase',lambda:followup.read(dbpath,folder/'index.db',{}));ex=timed('export_100_orders_combined',lambda:p.export_comparisons(dbpath,heads,folder,'combined'))
            if home['scope']!=1000 or len(receipt)!=100 or ex['cancelled'] or ex['error']:raise AssertionError('scale fixture outcome differs')
        finally:stop.set();thread.join()
        runs.append({'seconds':times,'peak_service_process_rss_mib':peak[0]/2**20})
    report={'passed':True,'synthetic':True,'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'scale':{'parent_orders':500,'warehouse_orders':1000,'signed_rows':50000,'purchase_rows':5000,'purchase_orders':100},'runs':runs,'seconds':{key:{'median':statistics.median(r['seconds'][key] for r in runs),'min':min(r['seconds'][key] for r in runs),'max':max(r['seconds'][key] for r in runs)} for key in runs[0]['seconds']},'measurement':'Same process/environment, sequential three runs, isolated SQLite copies. 20ms RSS sampling of this service process only; no GUI/spawn startup included. Filesystem cache not flushed; cold means new application projection cache only. New capabilities have no equivalent old-version benchmark; no speedup claimed.','python':sys.version}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report['seconds']))
if __name__=='__main__':main()
