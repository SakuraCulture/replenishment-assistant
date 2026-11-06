"""Reproducible isolated inventory fixture, performance, old-result parity and EXE checks."""
import argparse,copy,hashlib,json,os,pickle,sqlite3,statistics,subprocess,sys,time,zipfile
from contextlib import closing
from datetime import date,timedelta
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def save(p,x):p.write_text(json.dumps(x,ensure_ascii=False,indent=2,default=str),'utf-8',newline='\n')
def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def fixture(out,stores=2,rounds=2,skus=4):
    sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
    import test_inventory_structure as f,normal_orders as no,work_repository as wr
    case=f.ArchiveTests();case.setUp();(out/'settings').mkdir()
    target=out/'settings/workbench.sqlite3'
    try:
        with closing(sqlite3.connect(case.path)) as db,closing(sqlite3.connect(target)) as dest:db.backup(dest)
        with closing(sqlite3.connect(target)) as db,db:
            db.execute('DELETE FROM normal_orders');db.execute('DELETE FROM normal_revisions')
            for store in range(stores):
                for round_i in range(rounds):
                    day=(date.today()-timedelta(days=7*(rounds-round_i-1))).isoformat();payload=copy.deepcopy(case.f.p);rows=[]
                    for i in range(skus):
                        row=copy.deepcopy(f.sample()['rows'][i%4]);row.update(E=f'{i:06d}',A=f'合成门店{store}',B=f'S{store:03d}',K=row['K']+round_i,L=i%3);rows.append(row)
                    payload['result']['rows']=rows;payload['result']['store']=f'S{store:03d}';oid=f'inventory-{store}-{round_i}'
                    db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(day,f'S{store:03d}','normal_model',oid,'BH-'+oid,1))
                    db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',(oid,1,no.fingerprint(payload['result']),wr.now(),no.encode(payload)))
        spec={'from':(date.today()-timedelta(days=7*(rounds-1))).isoformat(),'to':date.today().isoformat()}
        save(out/'query.json',spec);save(out/'synthetic.json',{'synthetic':True,'stores':stores,'rounds':rounds,'skus_per_store_round':skus,'rows':stores*rounds*skus})
        return target,spec
    finally:case.tearDown()


def performance(a):
    sys.path.insert(0,str(ROOT/'src'));import inventory_structure as s,inventory_structure_exports as ex
    from verify_analytics_0390 import timed
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    if a.fixture:
        origin=a.fixture.resolve();meta=json.loads((origin/'synthetic.json').read_text('utf-8'))
        if not meta.get('synthetic'):raise ValueError('Only marked synthetic performance fixtures')
        db=origin/'settings/workbench.sqlite3';spec=json.loads((origin/'query.json').read_text('utf-8'))
        a.skus=meta['skus_per_store_round'];save(out/'synthetic.json',meta);save(out/'query.json',spec)
    else:db,spec=fixture(out,a.stores,a.rounds,a.skus)
    before=sha(db);runs=[]
    for i in range(3):
        report,q=timed(lambda:s.collect(db,spec));again,w=timed(lambda:s.collect(db,spec));first=report['rounds'][-1]
        detail,d=timed(lambda:s.detail(db,report,first['store'],first['day']))
        _,e=timed(lambda:ex.export(db,report,out/f'summary-{i}.xlsx'))
        # Full million-row detail export is explicit; row count remains under Excel sheet limit.
        _,full=timed(lambda:ex.export(db,report,out/f'details-{i}.xlsx',True)) if a.details else (None,None)
        import threading
        stop=threading.Event();stop.set();start=time.perf_counter()
        from runtime_support import Cancelled
        try:s.collect(db,spec,cancel=stop)
        except Cancelled:cancel_seconds=time.perf_counter()-start
        else:raise AssertionError('Cancellation ignored')
        runs.append({'query':q,'repeat_query':w,'detail':d,'summary_export':e,'detail_export':full,'cancel_seconds':cancel_seconds})
        print(json.dumps({'run':i+1,'query_seconds':q['seconds'],'repeat_seconds':w['seconds']},ensure_ascii=False),flush=True)
        if report['snapshot_id']!=again['snapshot_id'] or len(detail)!=a.skus:raise AssertionError('Repeat/detail mismatch')
    if sha(db)!=before:raise AssertionError('Database changed')
    keys=['query','repeat_query','detail','summary_export']+(['detail_export'] if a.details else [])
    summary={k:{'median':statistics.median(r[k]['seconds'] for r in runs),'min':min(r[k]['seconds'] for r in runs),'max':max(r[k]['seconds'] for r in runs),'peak_mib':max(r[k]['peak_sampled_rss_mb'] for r in runs)} for k in keys}
    save(out/'report.json',{'passed':True,'fixture':json.loads((out/'synthetic.json').read_text('utf-8')),'database_sha256':before,'runs':runs,'summary':summary,'limits':'Synthetic, same machine/Python, serial three runs. OS file cache warm after fixture generation. Each query includes SQLite reads, decoding, comparisons, aggregation; detail loaded on demand. RSS main process sampled every30ms including interpreter, short peaks may be missed. No baseline counterpart or speedup claim.'})
    print(json.dumps(summary))


def books_equal(a,b):
    import openpyxl
    from verify_coverage_0440 import strict
    from copy import copy
    books=[openpyxl.load_workbook(p) for p in (a,b)];count=0
    def cell(c):return (c.value,c.data_type,c.number_format,copy(c.font),copy(c.fill),copy(c.border),copy(c.alignment),copy(c.protection),c.comment.text if c.comment else None)
    try:
        strict(books[0].sheetnames,books[1].sheetnames)
        for name in books[0].sheetnames:
            x,y=books[0][name],books[1][name]
            strict((x.max_row,x.max_column,x.freeze_panes,x.print_title_rows,x.auto_filter.ref),(y.max_row,y.max_column,y.freeze_panes,y.print_title_rows,y.auto_filter.ref))
            for ra,rb in zip(x,y):strict([cell(c) for c in ra],[cell(c) for c in rb],name);count+=len(ra)
        return count
    finally:
        for b in books:b.close()


def compare(a):
    from verify_coverage_0440 import rich_fixture,strict
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);base=subprocess.check_output(['git','rev-parse',a.baseline],cwd=ROOT,text=True).strip()
    subprocess.run(['git','archive','-o',str(out/'baseline.zip'),base],cwd=ROOT,check=True)
    with zipfile.ZipFile(out/'baseline.zip') as z:z.extractall(out/'baseline')
    sample=out/'fixture';sample.mkdir();db,spec=rich_fixture(sample);records=[];cells=0;original=sha(db)
    for i in range(3):
        for kind in (('before','after') if i%2==0 else ('after','before')):
            source=out/'baseline' if kind=='before' else ROOT
            target=out/f'{kind}-{i}'
            subprocess.run([sys.executable,str(ROOT/'tools/verify_coverage_0440.py'),'worker','--source',str(source),'--database',str(db),'--spec',str(sample/'query.json'),'--output',str(target)],cwd=ROOT,check=True)
            records.append({'side':kind,'run':i,**json.loads(target.with_suffix('.json').read_text('utf-8'))})
        reports=[pickle.loads((out/f'{k}-{i}.pkl').read_bytes()) for k in ('before','after')]
        for r in reports:r.pop('as_of');r['history'].pop('as_of')
        strict(*reports);cells+=books_equal(out/f'before-{i}.xlsx',out/f'after-{i}.xlsx')
    if sha(db)!=original:raise AssertionError('Frozen input changed')
    save(out/'report.json',{'passed':True,'baseline':base,'differences':0,'cells_compared':cells,'dynamic_exclusions':['report.as_of','report.history.as_of'],'allowed_business_changes':[],'runs':records,'database_sha256':original})


def ui(a):
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);fixture(out)
    cmd=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    with (out/'stdout.log').open('w',encoding='utf-8') as log:
        p=subprocess.run(cmd+['--inventory-smoke',str(out)],cwd=ROOT,env={**os.environ,'REPLENISHMENT_DATA_DIR':str(out/'settings'),'PYTHONIOENCODING':'utf-8','QT_QPA_PLATFORM':'windows'},stdout=log,stderr=log,timeout=240)
    report=json.loads((out/'report.json').read_text('utf-8'));report['exit_code']=p.returncode
    if a.exe:report['exe_sha256']=sha(a.exe)
    save(out/'report.json',report);print(json.dumps(report,ensure_ascii=False))
    if p.returncode or not report['passed']:raise AssertionError('Actual UI failed')


if __name__=='__main__':
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='mode',required=True)
    x=sub.add_parser('performance');x.add_argument('--stores',type=int,default=10);x.add_argument('--rounds',type=int,default=10);x.add_argument('--skus',type=int,default=10000);x.add_argument('--details',action='store_true');x.add_argument('--fixture',type=Path)
    y=sub.add_parser('compare');y.add_argument('--baseline',required=True)
    z=sub.add_parser('ui');z.add_argument('--exe',type=Path)
    for cmd in (x,y,z):cmd.add_argument('--output',type=Path,required=True)
    a=p.parse_args();globals()[a.mode](a)
