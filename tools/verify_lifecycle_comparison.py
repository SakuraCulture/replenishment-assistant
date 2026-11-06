"""Re-runnable read-only equivalence and actual-EXE comparison export acceptance."""
import argparse,copy,hashlib,json,os,shutil,subprocess,sys,time
from pathlib import Path
from contextlib import closing
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'tools'),str(ROOT/'src'),str(ROOT/'tests')]
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def js(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')

def compare(qa):
    from parallel_qa import read,clean,diff,WHITELIST
    from structure_oracle import excel_snapshot,source_hashes
    old=qa/'before';new=qa/'after-final';a=json.loads((old/'capture.json').read_text('utf-8'));b=json.loads((new/'capture.json').read_text('utf-8'))
    if a['cases']!=b['cases'] or a['PYTHONHASHSEED']!='0' or b['PYTHONHASHSEED']!='0':raise ValueError('Mismatched capture settings')
    if b['source_hashes']!=source_hashes(ROOT):raise ValueError('Candidate changed after capture')
    cases=[]
    for name in a['cases']:cases.append({'case':name,'differences':diff(clean(read(old/(name+'.pklz'))),clean(read(new/(name+'.pklz'))))})
    for file in old.glob('*.xlsx'):cases.append({'case':file.name,'differences':diff(excel_snapshot(file),excel_snapshot(new/file.name))})
    frozen=json.loads((qa/'baseline.json').read_text('utf-8'));changed=[p for p,h in frozen['protected'].items() if sha(p)!=h]
    core={n:{'before':sha(qa/'baseline/src'/n),'after':sha(ROOT/'src'/n)} for n in ('model.py','one_box_exception.py','allocation.py','default_config.json','default_must_sell.json')}
    differences=sum(len(c['differences']) for c in cases);report={'passed':not differences and not changed and all(v['before']==v['after'] for v in core.values()),'baseline':frozen['baseline'],'cases':cases,'differences':differences,'whitelist':WHITELIST,'protected_count':len(frozen['protected']),'changed_protected':changed,'core':core,'source_hashes':b['source_hashes']};js(qa/'equivalence.json',report);print('equivalence',report['passed'],len(cases),differences)
    if not report['passed']:raise SystemExit(1)

def exe(args):
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    import test_lifecycle as fixtures,normal_orders as no,work_repository as wr
    data=fixtures.LifecycleTests();data.setUp()
    try:
        data.save('return',[{'门店确认数量':12}]);data.save('payment',[{'付款单商品数量':6,'实际金额':19.5}]);data.save('receipt',[{'实际收货数量':3}])
        payload=copy.deepcopy(data.payload);payload['result'].update(store='T2',store_name='另一测试店');digest=no.fingerprint(payload['result'])
        with closing(data.work.connect()) as db,db:
            db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(wr.today(),'T2','normal_model','oid2','BH-T2-ZC',1));db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',('oid2',1,digest,wr.now(),no.encode(payload, db)))
        shutil.copytree(data.path.parent,out/'settings')
    finally:data.tearDown()
    env=os.environ.copy();env.update(REPLENISHMENT_DATA_DIR=str(out/'settings'),PYTHONUTF8='1',QT_SCALE_FACTOR='1');env.pop('QT_QPA_PLATFORM',None)
    cmd=[str(args.exe.resolve())] if args.exe else [sys.executable,str(ROOT/'src/app.py')];started=time.perf_counter()
    with (out/'process.txt').open('w',encoding='utf-8') as f:proc=subprocess.run(cmd+['--lifecycle-comparison-smoke',str(out)],env=env,stdout=f,stderr=f,timeout=180)
    report=json.loads((out/'comparison-report.json').read_text('utf-8'));report.update(exit_code=proc.returncode,seconds=time.perf_counter()-started,exe_sha256=sha(args.exe) if args.exe else None);js(out/'comparison-report.json',report);print(json.dumps(report,ensure_ascii=False))
    if proc.returncode or not report['passed'] or args.exe and not report.get('frozen_exe'):raise SystemExit(1)

def legacy(args):
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    if args.source:
        # Fresh process: imports must resolve to the requested immutable version.
        sys.path.insert(0,str(args.source.resolve()/'src'))
        import history_read,order_lifecycle as lc,threading
        from structure_oracle import excel_snapshot
        heads=history_read.execute(args.db,out/'index.sqlite3',{'kind':'list'},threading.Event(),lambda _:None)['records']
        snapshots={};times={}
        for kind in ('comparison','return','payment','receipt'):
            eligible=heads if kind=='comparison' else [h for h in heads if lc.snapshot(args.db,h)['documents'][kind]]
            for mode in ('separate','combined'):
                t=time.perf_counter();result=lc.export(args.db,eligible,out,kind,mode);times[kind+'/'+mode]=time.perf_counter()-t
                if result['error'] or result['cancelled']:raise AssertionError(result)
                snapshots[kind+'/'+mode]=[excel_snapshot(Path(p)) for p in result['files']]
        js(out/'snapshot.json',snapshots);js(out/'times.json',times);return
    before=sha(args.db)
    for name,source in [('old',args.baseline.resolve()),('new',ROOT)]:
        subprocess.run([sys.executable,__file__,'legacy','--source',str(source),'--db',str(args.db.resolve()),'--output',str(out/name)],check=True)
    from parallel_qa import diff
    values=[json.loads((out/n/'snapshot.json').read_text('utf-8')) for n in ('old','new')];changes=diff(*values)
    report={'passed':not changes and sha(args.db)==before,'differences':changes,'cases':list(values[0]),'database_unchanged':sha(args.db)==before,'comparison':'ordered cells, types, worksheet order, format/style; original per-SKU lifecycle exports, four kinds x two modes; random filenames excluded only','times':{n:json.loads((out/n/'times.json').read_text('utf-8')) for n in ('old','new')}}
    js(out/'report.json',report);print(json.dumps(report,ensure_ascii=False))
    if not report['passed']:raise SystemExit(1)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['compare','exe','legacy']);p.add_argument('--qa',type=Path);p.add_argument('--output',type=Path);p.add_argument('--exe',type=Path);p.add_argument('--baseline',type=Path);p.add_argument('--source',type=Path);p.add_argument('--db',type=Path);a=p.parse_args()
    compare(a.qa.resolve()) if a.mode=='compare' else exe(a) if a.mode=='exe' else legacy(a)
