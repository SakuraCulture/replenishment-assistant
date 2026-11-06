"""Portable frozen-input AB/BA/AB commit benchmark, one warehouse and 10,000 SKUs.

Includes normal backup, validation, SQLite writes and event handling in commit.
Preparation and Excel validation run outside that timer. No user data is read.
"""
import argparse,copy,hashlib,json,os,shutil,sqlite3,statistics,subprocess,sys,time,zipfile
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def save(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def fixture(folder,count,skus):
    sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
    import test_jst,jst_orders as j,warehouse_catalog as wc,purchases,normal_orders as no,work_repository as wr
    f=test_jst.Service();f.setUp()
    try:
        heads=[f.h]
        for i in range(1,count):
            store=f'S{i:03}';oid='oid-'+store;p=copy.deepcopy(f.f.payload);p['result'].update(store=store,store_name='合成'+store)
            with closing(sqlite3.connect(f.path)) as db,db:
                db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(wr.today(),store,'normal_model',oid,'BH-'+store+'-ZC',1))
                db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',(oid,1,no.fingerprint(p['result']),wr.now(),no.encode(p, db)))
            h=next(h for h in purchases.targets(f.path) if h['store']==store and h['warehouse']=='河北仓')
            f.f.h=h;f.f.save('return',[{'门店编码':store,'门店确认数量':40}]);heads.append(h)
        f.f.h=f.h
        c=test_jst.catalog(stock=count*40+100);item=c['items']['0001']
        c['items']={f'{i:04}':{**item,'商品编码':f'{i:04}','_row':i+1} for i in range(1,skus+1)}
        c['revision']=wc.revision(c['items']);wc.Repository(f.path.parent).save(c)
        j.commit_refresh(f.path,j.prepare_refresh(f.path,'河北仓',catalog=c))
        plan=j.prepare_checked(f.path,heads,manual_order=True)
        folder.mkdir(parents=True);dbpath=folder/'workbench.sqlite3'
        with closing(sqlite3.connect(f.path)) as src,closing(sqlite3.connect(dbpath)) as dst:src.backup(dst)
        shutil.copytree(f.path.parent/'warehouse_catalogs',folder/'warehouse_catalogs')
        (folder/'plan.json').write_text(wr.dumps(plan),encoding='utf-8')
        save(folder/'frozen.json',{'synthetic':True,'orders':count,'sku':skus,'db_sha256':sha(dbpath),'plan_sha256':sha(folder/'plan.json'),'catalog_sha256':sha(folder/'warehouse_catalogs/河北仓.json')})
    finally:f.tearDown()

def worker(source,folder):
    sys.path.insert(0,str(source/'src'))
    import jst_orders as j,jst_exports as ex,warehouse_catalog as wc,work_repository as wr,openpyxl
    path=folder/'workbench.sqlite3';plan=wr.loads((folder/'plan.json').read_text('utf-8'));calls=[];connections=[]
    read=wc.Repository.catalogs;begin=j.lc.begin
    def tracked_begin(db,cancel):
        r=begin(db,cancel);connections.append(db);return r
    def tracked(repo):
        inside=False
        for db in connections:
            try:inside |= db.in_transaction
            except sqlite3.ProgrammingError:pass
        start=time.perf_counter()
        try:return read(repo)
        finally:calls.append({'seconds':time.perf_counter()-start,'inside_write_transaction':inside})
    wc.Repository.catalogs=tracked;j.lc.begin=tracked_begin
    start=time.perf_counter();receipt=j.commit(path,plan);elapsed=time.perf_counter()-start
    wc.Repository.catalogs=read;j.lc.begin=begin
    if receipt['errors'] or receipt['cancelled']:raise AssertionError(receipt)
    snapshots=[j.get(path,g) for g in receipt['generated']]
    with closing(sqlite3.connect(path)) as db:reservations=db.execute('SELECT gid,sku,quantity FROM jst_reservations ORDER BY rowid').fetchall()
    exported=ex.export(path,receipt['generated'],folder/'exports')
    if exported['failed']:raise AssertionError(exported)
    cells=[]
    for f in exported['files']:
        b=openpyxl.load_workbook(f['path']);cells.append({'name':Path(f['path']).name,'sheets':[(s.title,s.freeze_panes,[(k,d.width,d.hidden) for k,d in s.column_dimensions.items()],[[[c.value,c.data_type,c.number_format,str(c._style)] for c in row] for row in s]) for s in b]});b.close()
    report={'seconds':elapsed,'catalog_reads':len(calls),'catalog_read_seconds':sum(c['seconds'] for c in calls),'reads_in_write_transaction':sum(c['inside_write_transaction'] for c in calls),'raw_reads':calls,
            'business_sha256':hashlib.sha256(wr.dumps([snapshots,reservations]).encode()).hexdigest(),'excel_sha256':hashlib.sha256(wr.dumps(cells).encode()).hexdigest(),'generated':len(receipt['generated'])}
    save(folder/'report.json',report)

def run(out,baseline,skus):
    out.mkdir(parents=True,exist_ok=False);source=out/'baseline'
    archive=out/'baseline.zip';subprocess.run(['git','archive','--format=zip','-o',str(archive),baseline],cwd=ROOT,check=True)
    with zipfile.ZipFile(archive) as z:z.extractall(source)
    all_runs=[]
    for count in (1,10,30):
        seed=out/f'seed-{count}';fixture(seed,count,skus)
        for round_no,modes in enumerate((('old','new'),('new','old'),('old','new')),1):
            for mode in modes:
                dest=out/f'{count}-{round_no}-{mode}';dest.mkdir()
                for name in ('workbench.sqlite3','plan.json'):shutil.copy2(seed/name,dest/name)
                shutil.copytree(seed/'warehouse_catalogs',dest/'warehouse_catalogs')
                env={**os.environ,'PYTHONHASHSEED':'0','PYTHONIOENCODING':'utf-8'}
                with (dest/'process.txt').open('w',encoding='utf-8') as log:
                    subprocess.run([sys.executable,__file__,'worker','--source',str(source if mode=='old' else ROOT),'--out',str(dest)],env=env,stdout=log,stderr=log,check=True)
                record=json.loads((dest/'report.json').read_text('utf-8'));record.update(orders=count,round=round_no,mode=mode);all_runs.append(record)
        selected=[r for r in all_runs if r['orders']==count]
        if len({r['business_sha256'] for r in selected})!=1 or len({r['excel_sha256'] for r in selected})!=1:raise AssertionError('业务或Excel严格对照不一致')
    summary=[]
    for count in (1,10,30):
        for mode in ('old','new'):
            rs=[r for r in all_runs if r['orders']==count and r['mode']==mode];times=[r['seconds'] for r in rs]
            summary.append({'orders':count,'mode':mode,'median':statistics.median(times),'min':min(times),'max':max(times),'catalog_reads':[r['catalog_reads'] for r in rs],'reads_in_write_transaction':[r['reads_in_write_transaction'] for r in rs]})
    save(out/'report.json',{'passed':True,'baseline':baseline,'synthetic':True,'sku_per_warehouse':skus,'rounds':3,'execution':'serial AB/BA/AB; same Python; OS cache not cleared; not cold-disk or real store performance','timing_scope':'commit including backup; excludes plan preparation and export; no GUI responsiveness claim','business_differences':0,'excel_differences':0,'summary':summary,'runs':all_runs})

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['run','worker']);p.add_argument('--out',type=Path,required=True);p.add_argument('--source',type=Path);p.add_argument('--baseline',default='9f302f241c5979c1e23dc40d4ab55f96a5020d19');p.add_argument('--skus',type=int,default=10000);a=p.parse_args()
    if a.action=='worker':worker(a.source.resolve(),a.out.resolve())
    else:run(a.out.resolve(),a.baseline,a.skus)
