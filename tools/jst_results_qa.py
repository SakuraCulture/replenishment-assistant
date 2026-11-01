"""Reproducible isolated source/EXE and exact baseline export comparisons."""
import argparse,json,os,sys,shutil,subprocess,sqlite3,time,statistics
from pathlib import Path
from contextlib import closing
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests'),str(ROOT/'tools')]
from jst_qa import fixture,measure,sha,save


def exe(out,program):
    path=fixture(out);env=os.environ.copy()
    for k in ('PYTHONPATH','PYTHONHOME','QT_QPA_PLATFORM','QT_PLUGIN_PATH'):env.pop(k,None)
    env.update(REPLENISHMENT_DATA_DIR=str(path.parent),PYTHONHASHSEED='0',PYTHONIOENCODING='utf-8')
    if program:env['PATH']=env['WINDIR']+';'+str(Path(env['WINDIR'])/'System32')
    runs=[]
    for phase in ('exercise','reopen'):
        record=out/phase;record.mkdir()
        cmd=[str(program.resolve())] if program else [sys.executable,str(ROOT/'src/app.py')]
        result=measure(cmd+['--jst-results-smoke',str(out),phase],record,env)
        ui=json.loads((out/('results-ui-'+phase+'.json')).read_text(encoding='utf-8'));runs.append({**result,**ui})
        save(out/'report.json',{'passed':all(r['passed'] for r in runs),'actual_exe':program is not None,'exe_sha256':sha(program) if program else None,'runs':runs})
        if not ui['passed']:raise AssertionError(ui)


def worker(source,db,out,mode,gids):
    sys.path=[str(source/'src')]+[p for p in sys.path if p!=str(ROOT/'src')]
    import jst_exports,work_repository as wr
    from structure_oracle import excel_snapshot
    out.mkdir(parents=True,exist_ok=False)
    if mode=='old':r=jst_exports.export(db,json.loads(gids.read_text()),out)
    else:
        import jst_results
        data=jst_results.read(db,out/'index.sqlite3',wr.today());plan=jst_results.export_plan(data);r=jst_results.export(db,plan,out)
    if r['failed'] or r['cancelled']:raise AssertionError(r)
    values=[{'gid':f['gid'],'name':Path(f['path']).name,'sheet':excel_snapshot(f['path'])} for f in r['files']]
    save(out/'snapshot.json',values)


def comparison(out,baseline):
    import jst_orders as j,order_lifecycle as lc,warehouse_catalog as wc,jst_results
    out.mkdir(parents=True,exist_ok=False);db=fixture(out/'seed',31)
    for wh,c in wc.Repository(db.parent).catalogs().items():
        for row in c['items'].values():row['可用数']=31*48
        c['revision']=wc.revision(c['items']);j.commit_refresh(db,j.prepare_refresh(db,wh,catalog=c))
    receipt=j.commit(db,j.prepare(db,lc.current_targets(db)));gids=out/'gids.json';save(gids,receipt['generated'])
    frozen=sha(db);save(out/'inputs.json',{'db_sha256':frozen,'gids_sha256':sha(gids),'inputs':{str(p):sha(p) for p in (out/'seed').glob('*.xlsx')},'synthetic':True,'stores':31,'orders':62})
    for mode,source in [('old',baseline),('new',ROOT)]:
        dest=out/(mode+'.sqlite3');shutil.copy2(db,dest)
        subprocess.run([sys.executable,__file__,'worker','--source',str(source),'--db',str(dest),'--out',str(out/mode),'--mode',mode,'--gids',str(gids)],check=True)
    old=json.loads((out/'old/snapshot.json').read_text(encoding='utf-8'));new=json.loads((out/'new/snapshot.json').read_text(encoding='utf-8'))
    if old!=new or sha(db)!=frozen:raise AssertionError('ordered export/frozen input difference')
    # Absolute cold/warm view costs, serial and same input. No speedup claim.
    timings=[]
    for i in range(3):
        cache=out/f'measure-{i}.sqlite3';t=time.perf_counter();d=jst_results.read(db,cache,lc.wr.today());cold=time.perf_counter()-t
        t=time.perf_counter();jst_results.read(db,cache,lc.wr.today());warm=time.perf_counter()-t
        timings.append({'cold':cold,'warm':warm})
    save(out/'report.json',{'passed':True,'synthetic':True,'stores':31,'orders':62,'differences':0,'comparison':'exact ordered filenames, gid and all Excel values/types/styles/sheet/row/column order; existing clock/build-cell whitelist only, none of those cells in JST files','frozen_db_unchanged':sha(db)==frozen,'query_seconds':timings,'median_query_seconds':{k:statistics.median(r[k] for r in timings) for k in ('cold','warm')}})


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['exe','compare','worker']);p.add_argument('--out',type=Path,required=True);p.add_argument('--exe',type=Path);p.add_argument('--baseline',type=Path);p.add_argument('--source',type=Path);p.add_argument('--db',type=Path);p.add_argument('--mode');p.add_argument('--gids',type=Path);a=p.parse_args()
    if a.action=='exe':exe(a.out.resolve(),a.exe)
    elif a.action=='compare':comparison(a.out.resolve(),a.baseline.resolve())
    else:worker(a.source.resolve(),a.db.resolve(),a.out.resolve(),a.mode,a.gids.resolve())
