"""Synthetic 20-store, 10k-SKU, 30/90-day capacity QA. All writes in a fresh --output."""
import argparse,json,sys,time,threading,sqlite3,shutil
from pathlib import Path
from datetime import date,timedelta
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'qa/export_071/qa-deps')]


def save(path,data):path.write_text(json.dumps(data,ensure_ascii=False,indent=2),'utf-8')


def measure(repo,start,end,out):
    import psutil,stockout_metrics as m
    from stockout_exports import export
    proc=psutil.Process();stop=threading.Event();peaks=[proc.memory_info().rss,0]
    def sample():
        while not stop.wait(.03):
            peaks[0]=max(peaks[0],proc.memory_info().rss)
            # Only SQLite journal and analysis temporary files; never scan raw business data.
            peaks[1]=max(peaks[1],sum(p.stat().st_size for p in repo.folder.glob('*.sqlite3-journal') if p.exists()))
    sampler=threading.Thread(target=sample);sampler.start();io=proc.io_counters();cpu=sum(proc.cpu_times()[:2]);timings={}
    q={'start':start,'end':end,'view':'both'}
    def timed(name,fn):
        before=time.perf_counter();v=fn();timings[name]=time.perf_counter()-before;return v
    try:
        total=timed('summary',lambda:m.query(repo,q))
        trend=timed('trend',lambda:m.trend(repo,q))
        cuts=timed('supplier_category',lambda:m.cuts(repo,q,['supplier','category']))
        details=timed('detail_200',lambda:m.detail(repo,q,only_zero=True))
        assert len(details['items'])<=200
        assert sum(r['base'] for r in cuts['items'])==total['total']['base']
        assert sum(r['total']['zero'] for r in trend)==total['total']['zero']
        timed('summary_export',lambda:export(repo,q,out/'summary.xlsx',dimensions=['supplier','category']))
        cancel=threading.Event();timer=threading.Timer(.1,cancel.set);timer.start();before=time.perf_counter()
        try:m.cuts(repo,q,['sku','store'],cancel=cancel);raise AssertionError('Expected long query cancellation')
        except sqlite3.OperationalError as exc:
            assert 'interrupted' in str(exc);timings['cancel']=time.perf_counter()-before
        finally:timer.cancel()
        assert m.query(repo,q)['total']==total['total']
    finally:stop.set();sampler.join()
    endio=proc.io_counters()
    return {'days':(date.fromisoformat(end)-date.fromisoformat(start)).days+1,'rows':repo_rows(repo),
        'database_bytes':repo.path.stat().st_size,'peak_rss':peaks[0],'journal_peak':peaks[1],
        'io':{k:getattr(endio,k)-getattr(io,k) for k in io._fields},'cpu_seconds':sum(proc.cpu_times()[:2])-cpu,
        'timings':timings,'total':total['total'],'cuts':len(cuts['items']),'detail_count':len(details['items'])}


def repo_rows(repo):
    with repo.connect() as db:return db.execute('SELECT sum(rows) FROM store_snapshots').fetchone()[0]


def main(out,days,stores,skus):
    from stockout_repository import Repository
    from stockout_facts import load_catalog,load_store
    from stockout_common import digest,encoded,now,resource_check,file_hash
    import openpyxl
    out.mkdir(parents=True,exist_ok=False);save(out/'synthetic.json',{'synthetic':True,'stores':stores,'skus':skus,'days':days,'method':'One real XLSX projection, then isolated SQL expansion for historical scale; no platform calls'})
    repo=Repository(out/'settings');repo.initialize()
    names=[{'code':f'S{i:03d}','name':f'合成门店{i:03d}'} for i in range(stores)]
    basis=repo.save_settings({'stores':names,'routes':{s['code']:{'primary':'山东仓','secondary':None} for s in names},'profile':None})
    def book(path,heads,rows):
        b=openpyxl.Workbook(write_only=True);w=b.create_sheet();w.append(heads)
        for row in rows:w.append(row)
        b.save(path);b.close()
    catalog=out/'catalog.xlsx';sales=out/'sales.xlsx'
    book(catalog,['商品编码','商品状态','可用数','分类','供应商名称','虚拟分类'],
         ([f'P{i:06d}','启用',100,'类目'+str(i%10),'供货商'+str(i%20),'标杆淘汰' if i%10==0 else '淘便考核' if i%10==1 else ''] for i in range(skus)))
    book(sales,['仓库/门店编码','商品编码','商品名称','月销','可用库存','在途库存','补货状态'],
         (['S000',f'P{i:06d}','合成商品'+str(i),10,0 if i%4==0 else 5,2,'正常'] for i in range(skus)))
    begin=date(2026,8,1);t=time.perf_counter();sha=load_catalog(repo,catalog,'山东仓');cat={'山东仓':sha}
    run=repo.new_run(day=begin.isoformat());repo.change(run['id'],catalogs=cat)
    sid=load_store(repo,run['id'],'S000',sales,observed=begin.isoformat()+'T08:00:00+08:00')
    projection=time.perf_counter()-t;source_sha=file_hash(sales);reports=[];generation=time.perf_counter()
    for n in range(days):
        day=(begin+timedelta(days=n)).isoformat();resource_check(repo.folder,128*1024*1024)
        if n:
            run=repo.new_run(day=day);repo.change(run['id'],catalogs=cat)
        for s in names:
            if not n and s['code']=='S000':continue
            identity=digest(['synthetic',day,s['code']]);observed=day+'T08:00:00+08:00'
            with repo.connect(write=True) as db:
                db.execute('INSERT INTO store_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)',(identity,s['code'],s['name'],day,observed,source_sha,basis['version'],encoded(cat),skus,'READY'))
                db.execute('INSERT INTO fact_records SELECT ?,sku,name,month_raw,stock_raw,inbound_raw,month,stock,inbound,eligible,bad,zero,dimension,rownum,replen_status FROM fact_records WHERE snapshot=?',(identity,sid))
                db.execute('INSERT INTO summaries SELECT ?,view,base,zero,bad,unknown_tags,unknown_zero,removed_base,removed_zero FROM summaries WHERE snapshot=?',(identity,sid))
                db.execute('INSERT INTO dimension_summaries SELECT ?,dimension,replen_status,inbound_status,rows,eligible,zero,bad FROM dimension_summaries WHERE snapshot=?',(identity,sid))
            repo.store_result(run['id'],s['code'],identity)
        repo.publish(run['id'])
        if n+1 in {30,90,days}:
            folder=out/f'day-{n+1}';folder.mkdir();report=measure(repo,begin.isoformat(),day,folder);reports.append(report)
            save(folder/'report.json',report);print(json.dumps({'days':n+1,'rows':report['rows'],'database_mib':report['database_bytes']/2**20,'timings':report['timings']}),flush=True)
            if n+1==30 and days>30:
                # Retain a separate scale for cold-start comparisons; source analysis only.
                shutil.copyfile(repo.path,out/'30-day.sqlite3')
    result={'passed':True,'synthetic':True,'single_store_projection_seconds':projection,'elapsed_seconds':time.perf_counter()-generation,
            'reports':reports,'limits':'Synthetic fixed dimensions and SQL-expanded history. Query cost and storage scale, not 90 days of live acquisition; no low-spec PC or physical disk utilization claim.'}
    save(out/'report.json',result)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True,type=Path);p.add_argument('--days',type=int,default=90);p.add_argument('--stores',type=int,default=20);p.add_argument('--skus',type=int,default=10000)
    a=p.parse_args();main(a.output.resolve(),a.days,a.stores,a.skus)
