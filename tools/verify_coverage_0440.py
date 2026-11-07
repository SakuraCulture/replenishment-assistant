"""Frozen-baseline analytics parity, serial performance and isolated EXE acceptance."""
import argparse, copy, hashlib, json, os, pickle, sqlite3, statistics, subprocess, sys, time, zipfile
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def save(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2,default=str),'utf-8')


def rich_fixture(out):
    sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
    import test_order_analytics as fixtures, normal_orders as no, purchases, work_repository as wr
    f=fixtures.AnalyticsTests();f.setUp()
    try:
        for r in f.p['result']['rows']:r.update(AN=2,AM=1,K=4,L=6,AG=7,AH=3,G='包')
        f.save_payload();f.f.h=purchases.targets(f.path)[0]
        for s,key,q in [('return','门店确认数量',12),('payment','付款单商品数量',6),('receipt','实际收货数量',3)]:f.f.save(s,[{key:q}])
        oldday=(date.today()-timedelta(days=7)).isoformat()
        old=copy.deepcopy(f.p)
        for r in old['result']['rows']:r.pop('AN',None)
        with closing(sqlite3.connect(f.path)) as db,db:
            db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(oldday,'T','normal_model','old-coverage','BH-OLD-COVERAGE',1))
            db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',('old-coverage',1,no.fingerprint(old['result']),wr.now(),no.encode(old, db)))
        (out/'settings').mkdir()
        target=out/'settings/workbench.sqlite3'
        with closing(sqlite3.connect(f.path)) as db,closing(sqlite3.connect(target)) as dst:db.backup(dst)
        spec={'from':oldday,'to':wr.today()};save(out/'query.json',spec);save(out/'synthetic.json',{'synthetic':True})
        return target,spec
    finally:f.tearDown()


def worker(a):
    sys.path.insert(0,str(a.source/'src'))
    import order_analytics as analytics, order_analytics_exports as exports
    from verify_analytics_0390 import timed
    sys.path.insert(0,str(a.source/'src'))
    spec=json.loads(a.spec.read_text('utf-8'));before=sha(a.database)
    report,query_time=timed(lambda:analytics.collect(a.database,spec))
    target=a.output.with_suffix('.xlsx')
    receipt,export_time=timed(lambda:exports.export(a.database,report,target))
    save(a.output.with_suffix('.json'),{'query':query_time,'export':export_time,'rows':len(report['rows']),'database_unchanged':sha(a.database)==before,'receipt':receipt})
    a.output.with_suffix('.pkl').write_bytes(pickle.dumps(report))


def strict(a,b,path=''):
    if type(a) is not type(b):raise AssertionError('Type '+path)
    if isinstance(a,dict):
        if list(a)!=list(b):raise AssertionError('Keys/order '+path)
        for k in a:strict(a[k],b[k],path+'/'+str(k))
    elif isinstance(a,(list,tuple)):
        if len(a)!=len(b):raise AssertionError('Length '+path)
        for i,(x,y) in enumerate(zip(a,b)):strict(x,y,path+'/'+str(i))
    elif a!=b:raise AssertionError('Value '+path+' '+repr(a)+' / '+repr(b))


def compare_books(before,after):
    import openpyxl
    books=[openpyxl.load_workbook(p) for p in (before,after)];count=0
    def cell(c):return (c.value,c.data_type,c.number_format,str(c.font),str(c.fill),str(c.border),str(c.alignment),str(c.protection),c.comment.text if c.comment else None)
    try:
        if books[1].sheetnames[:len(books[0].sheetnames)]!=books[0].sheetnames:raise AssertionError('Sheet order')
        for name in books[0].sheetnames:
            old,new=books[0][name],books[1][name]
            if old.max_row!=new.max_row:raise AssertionError('Row count')
            if old.max_column!=37 or new.max_column!=42:raise AssertionError('Column contract')
            if old.freeze_panes!=new.freeze_panes or old.print_title_rows!=new.print_title_rows:raise AssertionError('View contract')
            for ra,rb in zip(old,new):
                strict([cell(c) for c in ra],[cell(c) for c in rb[:37]],name);count+=len(ra)
        return count
    finally:
        for b in books:b.close()


def compare(a):
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    baseline=subprocess.check_output(['git','rev-parse',a.baseline+'^{commit}'],cwd=ROOT,text=True).strip()
    archive=out/'baseline.zip';subprocess.run(['git','archive','-o',str(archive),baseline],cwd=ROOT,check=True)
    with zipfile.ZipFile(archive) as z:z.extractall(out/'baseline')
    protected={str(p):sha(p) for p in (ROOT/'交付_0.43.1_rc1/补货助手/补货助手.exe',ROOT/'交付_0.43.1_rc1/补货助手_0.43.1_rc.zip')}
    small=out/'rich';small.mkdir();db,spec=rich_fixture(small)
    from verify_analytics_0390 import fixture
    large=out/'large';large.mkdir();large_db,large_spec=fixture(large,stores=12,skus=500)
    # Freeze forecast fields before comparing; no model recalculation in analysis.
    sys.path.insert(0,str(ROOT/'src'));import normal_orders as no
    with closing(sqlite3.connect(large_db)) as conn,conn:
        for oid,rev,blob in conn.execute('SELECT oid,revision,payload FROM normal_revisions').fetchall():
            payload=no.decode(blob,conn)
            for r in payload['result']['rows']:r.update(AN=2,AM=1,K=4,L=6,AG=7,AH=3,G='包')
            conn.execute('UPDATE normal_revisions SET payload=?,digest=? WHERE oid=? AND revision=?',(no.encode(payload, conn),no.fingerprint(payload['result']),oid,rev))
    save(large/'query.json',large_spec)
    frozen={str(p):sha(p) for p in (db,large_db)};save(out/'frozen.json',{'baseline':baseline,'archive_sha256':sha(archive),'databases':frozen,'protected':protected})
    results={};cells=0
    for name,data in [('rich',small),('large',large)]:
        results[name]={'before':[],'after':[]}
        for i in range(3):
            for label in (('before','after') if i%2==0 else ('after','before')):
                source=out/'baseline' if label=='before' else ROOT;dest=data/f'{label}-{i}'
                cmd=[sys.executable,str(Path(__file__).resolve()),'worker','--source',str(source),'--database',str(data/'settings/workbench.sqlite3'),'--spec',str(data/'query.json'),'--output',str(dest)]
                with dest.with_suffix('.log').open('w',encoding='utf-8') as log:subprocess.run(cmd,cwd=ROOT,stdout=log,stderr=log,check=True,env={**os.environ,'PYTHONIOENCODING':'utf-8'})
                results[name][label].append(json.loads(dest.with_suffix('.json').read_text('utf-8')))
            old,new=[pickle.loads((data/f'{label}-{i}.pkl').read_bytes()) for label in ('before','after')]
            for report in (old,new):
                report.pop('as_of')
                report['history'].pop('as_of')
            if 'coverage' not in new:raise AssertionError('Missing extension')
            new.pop('coverage');strict(old,new)
            cells+=compare_books(data/f'before-{i}.xlsx',data/f'after-{i}.xlsx')
    if any(sha(Path(p))!=h for p,h in {**protected,**frozen}.items()):raise AssertionError('Protected data changed')
    summary={name:{label:{phase:{'median':statistics.median(r[phase]['seconds'] for r in runs),'min':min(r[phase]['seconds'] for r in runs),'max':max(r[phase]['seconds'] for r in runs),'sampled_peak_mb':max(r[phase]['peak_sampled_rss_mb'] for r in runs)} for phase in ('query','export')} for label,runs in versions.items()} for name,versions in results.items()}
    result={'passed':True,'baseline':baseline,'differences':0,'original_excel_cells_compared':cells,'allowed_changes':['report.coverage (new)','Excel appended AL:AP and new coverage sheets'],'dynamic_exclusions':['report.as_of','report.history.as_of'],'strict_types_and_order':True,'runs':results,'performance':summary,'limits':'Synthetic rich lifecycle and 12-store 6000-row inputs. Same Python/environment, serial AB/BA/AB, warm OS file cache. Query includes coverage projection; export includes freshness checks and save. RSS samples main process every30ms; includes interpreter, may miss short peaks. Original compact 37 columns compared including styles/comments; expanded filter range and new sheets allowed.'}
    save(out/'report.json',result);print(json.dumps({'passed':True,'cells':cells,'performance':summary},ensure_ascii=False))


def ui(a):
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);rich_fixture(out)
    cmd=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    with (out/'process.log').open('w',encoding='utf-8') as log:
        p=subprocess.run(cmd+['--coverage-smoke',str(out)],cwd=ROOT,env={**os.environ,'REPLENISHMENT_DATA_DIR':str(out/'settings'),'PYTHONIOENCODING':'utf-8','QT_QPA_PLATFORM':'windows'},stdout=log,stderr=log,timeout=240)
    r=json.loads((out/'report.json').read_text('utf-8'));r['exit_code']=p.returncode
    if a.exe:r['exe_sha256']=sha(a.exe)
    save(out/'report.json',r);print(json.dumps(r,ensure_ascii=False))
    if p.returncode or not r['passed'] or a.exe and not r['frozen_exe']:raise AssertionError('Actual UI failed')


if __name__=='__main__':
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='mode',required=True)
    x=sub.add_parser('compare');x.add_argument('--baseline',required=True)
    w=sub.add_parser('worker');w.add_argument('--source',type=Path);w.add_argument('--database',type=Path);w.add_argument('--spec',type=Path)
    u=sub.add_parser('ui');u.add_argument('--exe',type=Path)
    for cmd in (x,w,u):cmd.add_argument('--output',type=Path,required=True)
    args=p.parse_args();globals()[args.mode](args)
