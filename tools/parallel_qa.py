"""Independent fixed-0.6.1 comparison. Lists AND dictionary insertion order are strict."""
from __future__ import annotations
import argparse,copy,hashlib,json,os,pickle,shutil,subprocess,sys,threading,time,zlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
from structure_oracle import excel_snapshot,source_hashes

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def dump(p,x):Path(p).write_bytes(zlib.compress(pickle.dumps(x,protocol=5)))
def read(p):return pickle.loads(zlib.decompress(Path(p).read_bytes()))
def js(p,x):Path(p).write_text(json.dumps(x,ensure_ascii=False,indent=2,default=str),encoding='utf-8')

WHITELIST={'loaded.loaded_at':'wall clock','result.created':'wall clock','result.task.pid/started/finished':'process/timing','result.version.version/source_commit/build_id/built_at':'application build metadata','Excel':'only the exact clock/build cells enumerated by structure_oracle.excel_snapshot; no cells sorted, dropped or reordered'}
def clean(x):
    if isinstance(x,dict):
        result='rows' in x and 'summary' in x and 'store' in x;loaded='normalized' in x and 'config' in x
        out={}
        for k,v in x.items():
            if result and k=='created' or loaded and k=='loaded_at':v='<time>'
            elif result and k=='task':v={a:'<process>' if a in {'pid','started','finished'} else b for a,b in v.items()}
            elif result and k=='version':v={a:'<build>' if a in {'version','source_commit','build_id','built_at'} else b for a,b in v.items()}
            out[k]=clean(v)
        return out
    if isinstance(x,(list,tuple)):return type(x)(clean(v) for v in x)
    return x

def diff(a,b,path='$',out=None):
    out=[] if out is None else out
    if type(a)!=type(b):out.append([path,'type',repr(a)[:140],repr(b)[:140]]);return out
    if isinstance(a,dict):
        if list(a)!=list(b):out.append([path,'dictionary key order',repr(list(a))[:300],repr(list(b))[:300]])
        for k in a:
            if k in b:diff(a[k],b[k],path+'.'+str(k),out)
    elif isinstance(a,(list,tuple)):
        if len(a)!=len(b):out.append([path,'length',len(a),len(b)])
        for i in range(min(len(a),len(b))):diff(a[i],b[i],path+f'[{i}]',out)
    elif a!=b:out.append([path,'value',repr(a)[:140],repr(b)[:140]])
    return out

def freeze(q):
    inp=q/'inputs';inp.mkdir(exist_ok=False);baseline=q/'baseline';sys.path[:0]=[str(baseline/'src'),str(baseline)]
    import engine,warehouse_catalog,parameter_schema,openpyxl
    from verify_batch import write_fixture
    old=ROOT/'qa/structure_061/inputs'
    for n in ['补货建议导出服务.xlsx','混合仓商品资料.xlsx','商品资料_20261024103312_203178058_1.xlsx','sales.xlsx','河北仓.xlsx','山东仓.xlsx','edge.xlsx','edge_master.xlsx','conflict.xlsx','duplicate_header.xlsx','numeric_store.xlsx']:
        shutil.copy2(old/n,inp/n)
    shutil.copy2(baseline/'src/default_must_sell.json',inp/'must.json')
    standard=parameter_schema.standard();custom=copy.deepcopy(standard);custom['parameters']['probability']=.5;custom['weight_profiles']['快销'].update(alpha=.1,w1=.3,w2=.4)
    js(inp/'parameters.json',{'standard':standard,'custom':custom})
    js(inp/'catalogs.json',{w:warehouse_catalog.import_catalog(inp/(w+'.xlsx'),w) for w in warehouse_catalog.WAREHOUSES})
    sales=inp/'补货建议导出服务.xlsx';rows,info=engine.adapter.read_table(sales,engine.sheet_name(sales,'sales')[0],['商品编码']);headers=[k for k in rows[0] if k!='_row']
    for count in [2,6]:
        for i in range(count):write_fixture(inp/f'part{count}_{i}.xlsx',headers,rows[len(rows)*i//count:len(rows)*(i+1)//count])
    for i in range(3):write_fixture(inp/f'small{i}.xlsx',headers,rows[i*100:(i+1)*100])
    b=openpyxl.Workbook();b.active.append(headers);b.save(inp/'empty.xlsx');b.close()
    (inp/'broken.xlsx').write_bytes(b'not an xlsx')
    # Different first-seen store name and header appearance, same exact SKU content otherwise.
    edge,ei=engine.adapter.read_table(inp/'edge.xlsx',None,['商品编码']);other=copy.deepcopy(edge[:2]);other[0]['仓库/门店名称']='后文件店名';other[0]['可用库存']=7;other[1]['仓库/门店编码']='OTHER';other[1]['仓库/门店名称']='另一店';other[1]['追加字段']='额外'
    write_fixture(inp/'order.xlsx',[k for k in other[1] if k!='_row'],other)
    cases={'single':['edge.xlsx'],'nonoverlap':['part2_0.xlsx','part2_1.xlsx'],'overlap':['edge.xlsx','conflict.xlsx','order.xlsx'],'reversed':['order.xlsx','conflict.xlsx','edge.xlsx'],'path_dedup':['edge.xlsx','edge.xlsx','conflict.xlsx'],'mixed_error':['broken.xlsx','edge.xlsx','duplicate_header.xlsx','empty.xlsx','numeric_store.xlsx'],'all_error':['broken.xlsx','empty.xlsx','duplicate_header.xlsx','numeric_store.xlsx'],'warehouse':['sales.xlsx','sales.xlsx','order.xlsx']}
    js(inp/'cases.json',cases)
    protected={}
    for p in ROOT.glob('交付*'):
        if p.is_dir():protected.update({str(f):sha(f) for f in p.rglob('*') if f.is_file()})
    user=Path(os.environ['LOCALAPPDATA'])/'YoutuanTools/补货助手/settings';protected.update({str(p):sha(p) for p in user.rglob('*') if p.is_file()})
    original=Path('D:/企业微信/WXWork/1688855943717538/Cache/File/2026-10')
    protected.update({str(original/n):sha(original/n) for n in ['补货建议导出服务.xlsx','混合仓商品资料.xlsx','商品资料_20261024103312_203178058_1.xlsx']})
    js(q/'frozen.json',{'baseline_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),'source_hashes':source_hashes(baseline),'inputs':{str(p):sha(p) for p in inp.iterdir()},'protected':protected,'whitelist':WHITELIST,'fixtures':'Real 14244-row source/master. part2/part6/small are synthetic file partitions/excerpts of actual rows; order/edge/warehouse are labelled synthetic tests.'})

def capture(q,source,out,forced=False):
    out.mkdir(parents=True,exist_ok=False);sys.path[:0]=[str(source/'src'),str(source)];os.environ['REPLENISHMENT_DATA_DIR']=str(out/'settings')
    import engine as e,batch_service as bs
    inp=q/'inputs';cases=json.loads((inp/'cases.json').read_text(encoding='utf-8'));profile=json.loads((inp/'must.json').read_text(encoding='utf-8'));params=json.loads((inp/'parameters.json').read_text(encoding='utf-8'));catalogs=json.loads((inp/'catalogs.json').read_text(encoding='utf-8'));names=[]
    def put(name,x):dump(out/(name+'.pklz'),x);names.append(name)
    if forced:
        import file_intake,intake_probe,functools
        original=file_intake.checked_files;trace=out/'completion';trace.mkdir()
        task=functools.partial(intake_probe.probe_file,folder=str(trace),mode='reverse')
        file_intake.checked_files=lambda paths,progress,cancel:original(paths,progress,cancel,task=task)
    for name,files in cases.items():
        master=catalogs if name=='warehouse' else inp/('混合仓商品资料.xlsx' if name=='nonoverlap' else 'edge_master.xlsx')
        try:data=bs.intake([inp/f for f in files],master)
        except Exception as ex:put(name,{'error':(type(ex).__name__,str(ex))});continue
        put(name+'_intake',data)
        if data['conflicts']:
            put(name+'_blocked',bs.load([inp/f for f in files],master))
            for c in data['conflicts']:c['choice']=len(c['variants'])-1
        loaded=bs.prepare(data);put(name+'_loaded',loaded);results={}
        for i,s in enumerate(loaded['stores']):
            settings={'warehouses':e.DEFAULT_WAREHOUSES,'exception':True,'budget':None,'parameter_config':params['custom' if i%2 else 'standard']}
            if name=='warehouse':settings['supply_route']={'primary':'河北仓' if i%2==0 else '山东仓','secondary':'山东仓' if i%2==0 else '河北仓'}
            try:results[s]=bs.calculate_store(loaded,s,settings,profile)
            except ValueError as ex:results[s]={'error':str(ex)}
        put(name+'_results',results)
        valid={s:r for s,r in results.items() if 'rows' in r}
        if valid and name in {'overlap','reversed','warehouse'}:
            bs.export_batch(valid,{s:{'name':r['store_name'],'state':'已完成','error':''} for s,r in valid.items()},out/(name+'.xlsx'))
    real=bs.load([inp/'补货建议导出服务.xlsx'],inp/'混合仓商品资料.xlsx');put('real_loaded',real);store=next(iter(real['stores']))
    for limit,enabled in [(None,True),(0,True),(3000,True),(5000,True),(10000,True),(None,False)]:
        result=bs.calculate_store(real,store,{'warehouses':e.DEFAULT_WAREHOUSES,'exception':enabled,'budget':limit,'parameter_config':params['standard']},profile);put(f'real_{limit}_{enabled}',result)
        if limit==3000:e.export_xlsx(result,out/'real.xlsx');e.export_xlsx(result,out/'filtered.xlsx',[d for d in result['rows'] if d.get('_audit',{}).get('applied')],'当前筛选：一箱例外')
    put('real_custom',bs.calculate_store(real,store,{'warehouses':e.DEFAULT_WAREHOUSES,'exception':True,'budget':3000,'parameter_config':params['custom']},profile))
    js(out/'capture.json',{'cases':names,'source':str(source),'source_hashes':source_hashes(source),'forced':forced,'PYTHONHASHSEED':os.environ.get('PYTHONHASHSEED')})
    print('captured',len(names),out,flush=True)

def compare(q,a,b):
    names=json.loads((a/'capture.json').read_text(encoding='utf-8'))['cases'];assert names==json.loads((b/'capture.json').read_text(encoding='utf-8'))['cases'];cases=[]
    for name in names:
        changes=diff(clean(read(a/(name+'.pklz'))),clean(read(b/(name+'.pklz'))));cases.append({'case':name,'differences':len(changes),'details':changes[:50]})
    for file in a.glob('*.xlsx'):
        changes=diff(excel_snapshot(file),excel_snapshot(b/file.name));cases.append({'case':file.name,'differences':len(changes),'details':changes[:50]})
    frozen=json.loads((q/'frozen.json').read_text(encoding='utf-8'));changed=[p for p,h in {**frozen['inputs'],**frozen['protected']}.items() if not Path(p).is_file() or sha(p)!=h]
    cores=['model.py','one_box_exception.py','allocation.py','default_config.json','default_must_sell.json'];core={n:{'before':sha(q/'baseline/src'/n),'after':sha(ROOT/'src'/n)} for n in cores};assert all(v['before']==v['after'] for v in core.values())
    capture=json.loads((b/'capture.json').read_text(encoding='utf-8'));assert capture['source_hashes']==source_hashes(ROOT),'source changed since capture'
    count=sum(c['differences'] for c in cases);report={'version':json.loads((ROOT/'src/version.json').read_text(encoding='utf-8'))['version'],'passed':not count and not changed,'baseline_commit':frozen['baseline_commit'],'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),'candidate_sources':source_hashes(ROOT),'whitelist':WHITELIST,'strict_order':True,'differences':count,'cases':cases,'changed_protected':changed,'core':core};js(b/'equivalence.json',report);print(json.dumps({'passed':report['passed'],'differences':count}));assert report['passed']

def controls(q,out):
    out.mkdir(parents=True,exist_ok=False);sys.path.insert(0,str(ROOT/'src'));import file_intake,engine,intake_probe,functools,multiprocessing
    paths=[str((q/'inputs/edge.xlsx').resolve()),str((q/'inputs/order.xlsx').resolve()),str((q/'inputs/conflict.xlsx').resolve())];checks=[]
    for mode in ['reverse','cancel','crash']:
        trace=out/mode;trace.mkdir();task=functools.partial(intake_probe.probe_file,folder=str(trace),mode=mode);cancel=threading.Event();timer=threading.Timer(.4,cancel.set) if mode=='cancel' else None
        if timer:timer.start()
        try:
            results=list(file_intake.checked_files(paths,cancel=cancel,task=task));assert mode=='reverse';assert [r['index'] for r in results]==[0,1,2]
        except engine.Cancelled:assert mode=='cancel'
        except Exception as ex:assert mode=='crash' and type(ex).__name__=='BrokenProcessPool',repr(ex)
        if timer:timer.join()
        assert not multiprocessing.active_children()
        if mode=='reverse':assert json.loads((trace/'1_finish.json').read_text())['time']<json.loads((trace/'0_finish.json').read_text())['time']
        if mode=='cancel':assert not (trace/'2_start.json').exists()
        checks.append(mode+' passed; no live children')
    assert len(list(file_intake.checked_files(paths[:2])))==2;checks.append('retry after cancel/crash succeeds')
    # Real bytes change on a private copy at the read/check boundary, both versions.
    volatile=q/'volatile.xlsx';shutil.copy2(q/'inputs/edge.xlsx',volatile)
    original=intake_probe.mutate_reader(volatile)
    try:result=file_intake.check_file(0,str(volatile));assert result['report']['error']=='读取期间文件变化，请保存后重试。'
    finally:engine.adapter.read_table=original;volatile.unlink()
    checks.append('actual file bytes change after read detected on private copy')
    # Independently compare full mixed-file intake against frozen serial source,
    # with an actual write to the same disposable path after read in each run.
    volatile=q/'volatile.xlsx';shutil.copy2(q/'inputs/edge.xlsx',volatile)
    script=out/'baseline_change.py'
    script.write_text("import sys,pickle,zlib\nfrom pathlib import Path\nsys.path[:0]="+repr([str(q/'baseline/src'),str(ROOT/'src')])+"\nimport batch_service,intake_probe\nintake_probe.mutate_reader("+repr(str(volatile))+ ")\nx=batch_service.intake("+repr([str(volatile),paths[0]])+","+repr(str(q/'inputs/edge_master.xlsx'))+")\nPath("+repr(str(out/'baseline_change.pklz'))+").write_bytes(zlib.compress(pickle.dumps(x,protocol=5)))\n",encoding='utf-8')
    subprocess.run([sys.executable,str(script)],check=True,cwd=ROOT)
    shutil.copy2(q/'inputs/edge.xlsx',volatile)
    import batch_service
    original_files=file_intake.checked_files;trace=out/'change';trace.mkdir();task=functools.partial(intake_probe.probe_file,folder=str(trace),mode='change')
    file_intake.checked_files=lambda paths,progress,cancel:original_files(paths,progress,cancel,task=task)
    try:
        current=batch_service.intake([str(volatile),paths[0]],q/'inputs/edge_master.xlsx');changes=diff(read(out/'baseline_change.pklz'),current);assert not changes,changes;dump(out/'candidate_change.pklz',current)
    finally:file_intake.checked_files=original_files;volatile.unlink()
    checks.append('physical read-time file mutation: full mixed-error intake exactly equals independent frozen serial version')
    js(out/'controls.json',{'passed':True,'checks':checks,'version':json.loads((ROOT/'src/version.json').read_text(encoding='utf-8'))['version'],'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()});print(checks)

def main():
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['freeze','capture','compare','controls']);p.add_argument('--qa',type=Path,required=True);p.add_argument('--source',type=Path,default=ROOT);p.add_argument('--output',type=Path);p.add_argument('--baseline',type=Path);p.add_argument('--forced',action='store_true');a=p.parse_args();q=a.qa.resolve()
    if a.mode=='freeze':freeze(q)
    elif a.mode=='capture':capture(q,a.source.resolve(),a.output.resolve(),a.forced)
    elif a.mode=='compare':compare(q,a.baseline.resolve(),a.output.resolve())
    else:controls(q,a.output.resolve())
if __name__=='__main__':
    import multiprocessing
    multiprocessing.freeze_support();main()
