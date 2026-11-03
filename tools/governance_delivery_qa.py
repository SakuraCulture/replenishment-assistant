"""Independent frozen-source merchant comparison and sequential 3-pair timing.

Only reads frozen QA input/results. Every database/output is private to a run.
Usage: python tools/governance_delivery_qa.py run --name delivery-final
"""
import argparse,copy,hashlib,json,os,pickle,statistics,subprocess,sys,threading,time,zlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/governance_032'

def read(p):return pickle.loads(zlib.decompress(p.read_bytes()))
def save(p,value):p.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str),'utf-8')

def worker(source,out):
    out.mkdir(parents=True,exist_ok=False);os.environ['REPLENISHMENT_DATA_DIR']=str(out/'settings');sys.path[:0]=[str(source/'src'),str(source)]
    import batch_service as bs,engine,work_repository as wr,normal_orders as no,normal_delivery as nd,normal_exports as ne
    sys.path.append(str(ROOT/'qa/export_071/qa-deps'))
    import psutil
    loaded=read(QA/'before/real_loaded.pklz');store=next(iter(loaded['stores']));config=json.loads((QA/'inputs/parameters.json').read_text('utf-8'))['standard'];profile=json.loads((QA/'inputs/must.json').read_text('utf-8'))
    measures={};memory=[];stop=threading.Event();proc=psutil.Process()
    def sample():
        while not stop.is_set():
            total=0
            for p in [proc,*proc.children(recursive=True)]:
                try:total+=p.memory_info().rss
                except (psutil.NoSuchProcess,psutil.AccessDenied):pass
            memory.append(total);stop.wait(.05)
    watcher=threading.Thread(target=sample,daemon=True);watcher.start()
    def timed(name,fn):
        t=time.perf_counter();v=fn();measures[name]=time.perf_counter()-t;return v
    try:
        result=timed('calculation',lambda:bs.calculate_store(loaded,store,{'warehouses':engine.DEFAULT_WAREHOUSES,'exception':True,'budget':None,'parameter_config':config},profile))
        work=wr.Repository(out/'settings');repo=no.Repository(work)
        import new_product_focus
        focus=new_product_focus.empty(wr.today()[:7])
        payload={'schema':1,'day':wr.today(),'model_version':no.MODEL_VERSION,'input_dirty':False,'loaded':loaded,'new_focus':focus,'results':{store:result},'states':{store:{'state':'已完成','name':result['store_name']}}}
        plan=timed('prepare',lambda:nd.prepare_batch(repo,payload,[store]))
        orders=timed('archive',lambda:repo.confirm(plan['payload'],[store],None,{store:None}))
        order=orders[0]
        # Identical reference metadata in both source processes. This does not
        # rewrite the private archive; it only fixes dynamic export audit text.
        order={**order,'saved':'2026-11-03T00:00:00','digest':'fixed-reference-digest'}
        from warehouse_orders import summaries
        warehouses=[r['warehouse'] for r in summaries(result,focus)]
        def export():
            for i,warehouse in enumerate(warehouses):ne.export(order,out/f'merchant-{i}.xlsx',warehouse=warehouse)
        timed('export',export)
        timed('unchanged_preview',lambda:nd.prepare_batch(repo,payload,[store]))
        if (source/'src/admission.py').exists():
            import admission
            context=admission.snapshot(work.path,store)
            # Synthetic nonmedical tag evidence isolates pure guard cost, rather
            # than pretending a historical file had modern tag fields.
            probe=copy.deepcopy(loaded)
            for d,m in probe['normalized']:m.update(product_tags='',product_tags_known=True,medical_sources=[])
            timed('admission_guard_synthetic_tags',lambda:admission.apply(probe,store,context))
        save(out/'result.json',{'source':str(source),'times':measures,'rows':len(result['rows']),'summary':result['summary'],'rss_peak_bytes':max(memory),'rss_initial_bytes':memory[0],'memory_sample_seconds':.05,'warehouses':warehouses})
    finally:stop.set();watcher.join()

def snapshot(path,new):
    import openpyxl
    from xml.etree.ElementTree import tostring
    b=openpyxl.load_workbook(path);sheets=[]
    def style(x):return tostring(x.to_tree(),encoding='unicode')
    for ws in b:
        if new and ws.title=='系统关联信息':continue
        indices=list(range(1,21))+([21] if new else [22])
        rows=[]
        for rn in range(1,ws.max_row+1):
            row=[]
            for cn in indices:
                c=ws.cell(rn,cn)
                row.append({'value':c.value,'python_type':type(c.value).__name__,'data_type':c.data_type,'format':c.number_format,'font':style(c.font),'fill':style(c.fill),'border':style(c.border),'alignment':style(c.alignment),'protection':style(c.protection),'comment':(c.comment.text,c.comment.author) if c.comment else None})
            rows.append(row)
        sheets.append({'name':ws.title,'rows':rows,'freeze':ws.freeze_panes,'merged':str(ws.merged_cells),'row_dimensions':[(i,v.height,v.hidden) for i,v in ws.row_dimensions.items()],'columns':[(openpyxl.utils.get_column_letter(i),ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width,ws.column_dimensions[openpyxl.utils.get_column_letter(i)].hidden) for i in range(1,21)],'page_setup':style(ws.page_setup),'print_options':style(ws.print_options),'header':str(ws.oddHeader)})
    b.close();return sheets

def run(name):
    from parallel_qa import diff
    out=QA/name;out.mkdir(exist_ok=False);records=[];differences=[]
    for n in range(3):
        order=['baseline','candidate'] if n%2==0 else ['candidate','baseline']
        for label in order:
            source=QA/'baseline' if label=='baseline' else ROOT;target=out/f'{label}-{n+1}'
            subprocess.run([sys.executable,__file__,'worker','--source',str(source),'--output',str(target)],cwd=ROOT,check=True,env={**os.environ,'PYTHONIOENCODING':'utf-8','PYTHONHASHSEED':'0'})
            records.append({'label':label,'round':n+1,**json.loads((target/'result.json').read_text('utf-8'))});print(label,n+1,records[-1]['times'],flush=True)
        a,b=out/f'baseline-{n+1}',out/f'candidate-{n+1}'
        old=list(a.glob('merchant-*.xlsx'));new=list(b.glob('merchant-*.xlsx'))
        if [p.name for p in old]!=[p.name for p in new]:raise RuntimeError('Export file order changed')
        for p in old:differences+=diff(snapshot(p,False),snapshot(b/p.name,True),f'round{n+1}.{p.name}')
        differences+=diff(json.loads((a/'result.json').read_text('utf-8'))['summary'],json.loads((b/'result.json').read_text('utf-8'))['summary'],f'round{n+1}.summary')
    table={}
    for label in ['baseline','candidate']:
        values=[r for r in records if r['label']==label];table[label]={}
        for metric in [*values[0]['times'],'rss_peak_bytes']:
            data=[r['rss_peak_bytes'] if metric=='rss_peak_bytes' else r['times'][metric] for r in values]
            table[label][metric]={'all':data,'median':statistics.median(data),'min':min(data),'max':max(data)}
    report={'passed':not differences,'baseline_commit':json.loads((QA/'baseline.json').read_text('utf-8'))['commit'],'candidate_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'records':records,'table':table,'differences':differences,'allowed_changes':['merchant visible order-number column U removed; previous decision V now U','new veryHidden identity worksheet and workbook structure protection; deterministic identity metadata','existing internal/warehouse exports not projected in this comparison; separate strict 38-case report'], 'comparison':'All cells A:T and old V -> new U: strict value/Python type/Excel type/styles/comments, sheet and row order; original A:T widths plus row dimensions and print settings. No row sorting. Original reference number/digest/time fixed identically only in private export descriptor.', 'environment':{'python':sys.version,'platform':sys.platform,'memory':'Parent plus recursive children RSS sum every 50ms, shared pages may double-count; excludes Python imports and fixture decode before measurement. Archive and export included.','cache':'No cache flush. Alternating serial order, same interpreter/dependencies/machine; warm OS file cache, not cold-start claim.','data':'14244-row frozen real sales/master; calculation/prepare/archive/export isolated. Separate admission-only timing uses copied rows with synthetic blank nonmedical tags; not real qualification assertion.'}}
    save(out/'report.json',report)
    if differences:raise SystemExit(1)

if __name__=='__main__':
    import multiprocessing
    multiprocessing.freeze_support();p=argparse.ArgumentParser();p.add_argument('mode',choices=['run','worker']);p.add_argument('--name',default='delivery-final');p.add_argument('--source',type=Path);p.add_argument('--output',type=Path);a=p.parse_args()
    if a.mode=='run':run(a.name)
    else:worker(a.source,a.output)
