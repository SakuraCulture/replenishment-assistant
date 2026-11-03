"""Frozen synthetic input, independent source processes, exact ordered comparison.

Run with the project's venv; never reads normal user settings or online accounts.
"""
import argparse,copy,hashlib,json,os,shutil,sqlite3,statistics,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def save(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2),encoding='utf-8')

def worker(source,folder,new):
    sys.path.insert(0,str(source/'src'))
    import jst_orders as j,jst_exports as ex,warehouse_catalog as wc,work_repository as wr
    from jst_results_smoke import cells
    path=folder/'workbench.sqlite3';seed=wr.loads((folder/'plan.json').read_text('utf-8'))
    heads=[p['head'] for p in seed['payload']['plans']];start=time.perf_counter()
    if new:
        import jst_batches as b
        plan=b.preview_new(path,heads,wc.Repository(folder).catalogs(),True)
    else:plan=j.prepare_checked(path,heads,True)
    prepared=time.perf_counter()
    result=b.execute(path,plan) if new else j.commit(path,plan)
    committed=time.perf_counter()
    if result['errors'] or result['cancelled']:raise AssertionError(result)
    exported=ex.export_checked(path,result['generated'],folder/'exports')
    finished=time.perf_counter()
    if exported['failed']:raise AssertionError(exported)
    values=[]
    for gid in result['generated']:
        p=j.get(path,gid)
        # Exact whitelist: random task/baseline ids, timestamp, new batch audit.
        for key in ('gid','operation','bid','saved','batch_id','batch_epoch'):p.pop(key,None)
        values.append(p)
    xlsx=[{'filename':Path(f['path']).name,'cells':cells(f['path'])} for f in exported['files']]
    save(folder/'result.json',{'business':values,'excel':xlsx,'seconds':{'prepare':prepared-start,'save':committed-prepared,'export':finished-committed,'total':finished-start}})

def main():
    p=argparse.ArgumentParser();p.add_argument('--baseline',type=Path);p.add_argument('--out',type=Path,required=True);p.add_argument('--worker',type=Path);p.add_argument('--new',action='store_true');a=p.parse_args()
    if a.worker:return worker(a.worker.resolve(),a.out.resolve(),a.new)
    from verify_current_basis import fixture
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False);frozen=out/'frozen';fixture(frozen,10,10000)
    baseline=a.baseline.resolve();rounds=[]
    for i,sequence in enumerate((('old','new'),('new','old'),('old','new')),1):
        values={}
        for name in sequence:
            dest=out/f'{i}-{name}';shutil.copytree(frozen,dest)
            cmd=[sys.executable,str(Path(__file__).resolve()),'--worker',str(ROOT if name=='new' else baseline),'--out',str(dest)]
            if name=='new':cmd.append('--new')
            subprocess.run(cmd,check=True,env={**os.environ,'PYTHONIOENCODING':'utf-8'})
            values[name]=json.loads((dest/'result.json').read_text('utf-8'))
        for field in ('business','excel'):
            if values['old'][field]!=values['new'][field]:raise AssertionError(f'round {i}: ordered {field} differs; inspect both result.json files')
        rounds.append({name:values[name]['seconds'] for name in ('old','new')})
    report={'passed':True,'synthetic':True,'orders':10,'warehouse_sku':10000,'business_differences':0,'excel_differences':0,
        'whitelist':['generation.gid','generation.operation','generation.bid','generation.saved','generation.batch_id (new audit)','generation.batch_epoch (new audit)'],
        'scope':'All remaining generation fields, ordered records; Excel filename, sheet/row/column order, values, types, number formats, styles; no sorting or numeric tolerance.',
        'raw_seconds':rounds,'summary':{name:{stage:{'median':statistics.median(r[name][stage] for r in rounds),'min':min(r[name][stage] for r in rounds),'max':max(r[name][stage] for r in rounds)} for stage in ('prepare','save','export','total')} for name in ('old','new')},
        'limits':'Warm OS cache; synthetic 10 orders/10000 warehouse SKU; no real platform operations or peak memory benchmark. Single-batch equivalence only; new-batch non-inheritance is a deliberate tested change.'}
    save(out/'report.json',report);print(json.dumps(report,ensure_ascii=False))

if __name__=='__main__':main()
