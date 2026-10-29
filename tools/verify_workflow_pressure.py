"""Real 5/25-file pressure entry; isolated UI data, sequential runs and RSS supervision."""
import argparse,hashlib,json,os,shutil,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/full_workflow_073'

def freeze():
    sys.path.insert(0,str(ROOT/'src'));import work_repository as wr,new_product_focus as focus
    folder=QA/'pressure_inputs';folder.mkdir(exist_ok=False)
    inventory=json.loads((QA/'inputs_manifest.json').read_text(encoding='utf-8'));settings=wr.loads((QA/'large_probe_new_fast/frozen_settings.json').read_text(encoding='utf-8'));paths=[];frozen=[]
    for row in inventory:
        p=Path(row['path']);assert hashlib.sha256(p.read_bytes()).hexdigest()==row['sha256'];target=folder/p.name;shutil.copy2(p,target);paths.append(str(target));frozen.append({'original':str(p),'copy':str(target),'sha256':row['sha256'],'rows':row['rows'],'stores':list(row['names'])})
    primary=settings['store_ui'][next(iter(settings['store_ui']))]['primary']
    for name,items in [('five',[x for x in frozen if any(s in settings['store_ui'] for s in x['stores'])]),('all',frozen)]:
        spec={**settings,'paths':[x['copy'] for x in items],'expected_rows':sum(x['rows'] for x in items),'expected_stores':sum(len(x['stores']) for x in items),'extra_store_ui':{'mode':1,'amount':'','primary':primary,'secondary':None,'selected':True},'new_focus':focus.empty(wr.today()[:7]),'label':'Actual source rows and frozen warehouse/must-list. Original five stores keep saved parameters/budgets/routes; additional entities use explicit QA standard/no budget/same primary. Empty monthly focus is a QA setting, not a user configuration change.'}
        (QA/('pressure_'+name+'.json')).write_text(wr.dumps(spec),encoding='utf-8')
    (QA/'pressure_manifest.json').write_text(json.dumps(frozen,ensure_ascii=False,indent=2),encoding='utf-8')

def launch(a):
    sys.path.insert(0,str(ROOT/'qa/export_071/qa-deps'));import psutil
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);env=os.environ.copy()
    for key in ['PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','QT_QPA_PLATFORM']:env.pop(key,None)
    env['PATH']=str(Path(env['WINDIR'])/'System32')+';'+env['WINDIR'];env['REPLENISHMENT_DATA_DIR']=str(out/'settings');env['PYTHONHASHSEED']='0'
    if not a.exe:env['QT_QPA_PLATFORM']='offscreen'
    command=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    with (out/'run.log').open('w',encoding='utf-8') as log:
        run=subprocess.Popen(command+['--workflow-smoke',str(QA/('pressure_'+a.scope+'.json')),str(out)],env=env,cwd=out,stdout=log,stderr=log);proc=psutil.Process(run.pid);peaks={};children=set();started=time.monotonic()
        while run.poll() is None:
            if time.monotonic()-started>1800:
                for child in proc.children(recursive=True):child.kill()
                proc.kill();raise TimeoutError('pressure QA only')
            try:
                phase=(out/'phase.txt').read_text(encoding='utf-8') if (out/'phase.txt').exists() else 'startup';tree=proc.children(recursive=True);children.update((p.pid,p.create_time()) for p in tree);rss=sum(p.memory_info().rss for p in [proc]+tree);peaks[phase]=max(peaks.get(phase,0),rss)
            except (psutil.Error,OSError):pass
            time.sleep(.05)
    report=json.loads((out/'workflow_report.json').read_text(encoding='utf-8'));left=[]
    for pid,created in children:
        try:
            c=psutil.Process(pid)
            if c.create_time()==created and c.is_running():left.append(pid)
        except psutil.Error:pass
    report.update(exit_code=run.returncode,remaining_children=left,peak_tree_mib={k:v/2**20 for k,v in peaks.items()},memory_method='external supervisor, 50ms sum of parent and owned descendant RSS; shared pages double-counted; includes fixed full input/results; final capture is QA evidence overhead',development=not bool(a.exe),exe_sha256=hashlib.sha256(a.exe.read_bytes()).hexdigest() if a.exe else None)
    (out/'workflow_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({'passed':report['passed'],'timings':report['timings'],'peak_tree_mib':max(report['peak_tree_mib'].values()),'remaining_children':left},ensure_ascii=False));assert run.returncode==0 and report['passed'] and not left

def baseline(a):
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);sys.path[:0]=[str(QA/'baseline/src'),str(ROOT/'src')];os.environ['REPLENISHMENT_DATA_DIR']=str(out/'settings')
    import batch_service as bs,work_repository as wr
    from workflow_smoke import capture
    import pickle,zlib
    spec=wr.loads((QA/('pressure_'+a.scope+'.json')).read_text(encoding='utf-8'));loaded=bs.load(spec['paths'],spec['catalogs'])
    # The JSON file is human-readable; use the separately captured actual input
    # settings to preserve dict insertion order and tuples in the strict oracle.
    settings={s:pickle.loads(zlib.decompress((a.candidate/'capture'/(s+'.pklz')).read_bytes()))['effective_settings'] for s in loaded['stores']}
    results=bs.run(loaded,settings,spec['profile'],4)
    capture(loaded,results,settings,out/'capture')

def compare(a):
    sys.path.insert(0,str(ROOT/'tools'));from parallel_qa import read,clean,diff
    old=a.baseline/'capture';new=a.candidate/'capture';cases=[]
    assert {p.name for p in old.glob('*.pklz')}=={p.name for p in new.glob('*.pklz')}
    for path in old.glob('*.pklz'):
        before=clean(read(path));after=clean(read(new/path.name))
        # Frozen diagnostics add built_at, absent in source runs. Its presence
        # and value are the already-declared nonbusiness build leaf, not a
        # licence to ignore the version object or any model/schema fields.
        for value in [before,after]:
            if 'rows' in value and 'summary' in value:
                value['version']={k:v for k,v in value['version'].items() if k not in ('version','source_commit','build_id','built_at')}
        delta=diff(before,after);cases.append({'file':path.name,'differences':len(delta),'details':delta[:30]})
    report={'passed':not any(x['differences'] for x in cases),'baseline_commit':'84a0671bd81daa8c2e94a8648c2332b9fc8fa2a9','cases':cases,'comparison':'intake exact ordered per-record pickle signatures and all reports/order; each store full values/types/key/list order via strict recursive comparator; process completion notification order is recorded independently and is not the business row order','whitelist':['result.created','result.task.pid/started/finished','result.version.version/source_commit/build_id/built_at']}
    (a.candidate/'equivalence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False));assert report['passed']

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['freeze','run','baseline','compare']);p.add_argument('--scope',choices=['five','all'],default='five');p.add_argument('--output',type=Path);p.add_argument('--exe',type=Path);p.add_argument('--candidate',type=Path);p.add_argument('--baseline',type=Path);a=p.parse_args()
    if a.mode=='freeze':freeze()
    elif a.mode=='run':launch(a)
    elif a.mode=='baseline':baseline(a)
    else:compare(a)
