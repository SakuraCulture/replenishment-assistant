"""0.18.1 independent old/new capture; all writes belong to supplied QA directory.

Freeze baseline with git archive before modification. QA inputs are copies of the
existing 14244-row regression set and read-only copies of warehouse catalog JSON.
Baseline oracle changes only the authorized selected pair, then runs OLD engine.
"""
import argparse,copy,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
from parallel_qa import clean,diff,dump,read,js,sha


def worker(source,q,out):
    sys.path[:0]=[str(source/'src'),str(source)]
    os.environ['REPLENISHMENT_DATA_DIR']=str(out/'settings');out.mkdir(parents=True,exist_ok=False)
    import engine as e,batch_service as bs,warehouse_catalog as wc
    inp=q/'inputs';profiles=json.loads((inp/'parameters.json').read_text(encoding='utf-8'))
    must=json.loads((inp/'must_sell.json').read_text(encoding='utf-8'));results={};eligibility={}
    def settings(p=None,s=None,budget=None,exception=True,custom=False):
        return {'warehouses':e.DEFAULT_WAREHOUSES,'exception':exception,'budget':budget,
                'parameter_config':profiles['custom' if custom else 'standard'],
                **({'supply_route':{'primary':p,'secondary':s}} if p else {})}
    legacy=e.load_inputs(inp/'补货建议导出服务.xlsx',inp/'混合仓商品资料.xlsx');store=next(iter(legacy['stores']))
    for limit in (None,0,3000,5000,10000):
        results['legacy_'+str(limit)]=bs.calculate_store(legacy,store,settings(budget=limit),must)
    results['legacy_no_exception']=bs.calculate_store(legacy,store,settings(exception=False),must)
    results['legacy_custom']=bs.calculate_store(legacy,store,settings(budget=3000,custom=True),must)
    del legacy
    original=wc.route_store
    def oracle_route(loaded,store,route):
        routed=original(loaded,store,route);p=route['primary'];s=route.get('secondary')
        if s:
            mains=[x for x in loaded['warehouse_rows'][p] if x[0]['B']==store]
            subs=[x for x in loaded['warehouse_rows'][s] if x[0]['B']==store]
            for i,((md,mm),(sd,sm)) in enumerate(zip(mains,subs)):
                if mm['match']=='商品编码精确匹配' and sm['match']=='资料未匹配' and wc.assessment((md,mm)) in ('无货','禁用'):
                    d,m=copy.deepcopy((md,mm));routing=copy.deepcopy(routed['normalized'][i][1]['routing'])
                    routing.update(assigned=p,reason=f"主仓{wc.assessment((md,mm))}，从仓无该商品，保留主仓归属及资料",version='0.5.1')
                    m['routing']=routing;routed['normalized'][i]=(d,m)
        return routed
    for dataset in ('synthetic','real_saved_catalogs'):
        catalogs=json.loads((inp/'catalogs.json').read_text(encoding='utf-8')) if dataset=='synthetic' else {w:json.loads((q/'live_catalogs'/(w+'.json')).read_text(encoding='utf-8')) for w in wc.WAREHOUSES}
        loaded=bs.load([inp/('sales.xlsx' if dataset=='synthetic' else '补货建议导出服务.xlsx')],catalogs)
        for store in loaded['stores']:
            for primary,secondary in [('河北仓','山东仓'),('山东仓','河北仓'),('河北仓',None),('山东仓',None)]:
                limits=(None,0,5000) if dataset=='real_saved_catalogs' and secondary else (None,60) if secondary else (None,)
                p=[x for x in loaded['warehouse_rows'][primary] if x[0]['B']==store]
                s=[x for x in loaded['warehouse_rows'].get(secondary,[]) if x[0]['B']==store]
                keys=[d['E'] for i,(d,m) in enumerate(p) if secondary and m['match']=='商品编码精确匹配' and s[i][1]['match']=='资料未匹配' and wc.assessment((d,m)) in ('无货','禁用')]
                for limit in limits:
                    name=f'{dataset}/{store}/{primary}/{secondary}/{limit}';cfg=settings(primary,secondary,limit)
                    results[name]=bs.calculate_store(loaded,store,cfg,must);eligibility[name]=keys
                    if source==q/'baseline':
                        wc.route_store=oracle_route
                        try:results[name+'/oracle']=bs.calculate_store(loaded,store,cfg,must)
                        finally:wc.route_store=original
    dump(out/'results.pklz',results);js(out/'eligible.json',eligibility)
    js(out/'summary.json',{k:{'rows':len(r['rows']),'amount':r['summary']['amount'],'count':r['summary']['positive']} for k,r in results.items()})


def normalized(r):
    r=clean(r)
    # Application-level capability version only. Per-row routing version/reason
    # are business audit and MUST compare to the independently corrected oracle.
    r['version']['routing_version']='<application routing capability>'
    return r


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--qa',type=Path,required=True);ap.add_argument('--worker',type=Path);ap.add_argument('--out',type=Path);ap.add_argument('--compare-only',action='store_true')
    a=ap.parse_args();q=a.qa.resolve()
    if a.worker:return worker(a.worker.resolve(),q,a.out.resolve())
    env=os.environ.copy();env.update(PYTHONIOENCODING='utf-8',PYTHONHASHSEED='0')
    for label,src in ([] if a.compare_only else [('old',q/'baseline'),('new',ROOT)]):
        with (q/(label+'.log')).open('w',encoding='utf-8') as log:
            subprocess.run([sys.executable,__file__,'--qa',str(q),'--worker',str(src),'--out',str(q/label)],env=env,stdout=log,stderr=log,check=True)
    old=read(q/'old/results.pklz');new=read(q/'new/results.pklz');eligible=json.loads((q/'old/eligible.json').read_text(encoding='utf-8'))
    reports=[];failures=[];changes=[]
    for name,current in new.items():
        baseline=old[name];oracle=old.get(name+'/oracle',baseline)
        allowed=set(eligible.get(name,[]))
        ds=diff(normalized(oracle),normalized(current));failures.extend([[name]+d for d in ds])
        keys=lambda r:[(d['B'],d['E']) for d in r['rows']]
        if keys(current)!=keys(baseline) or len(keys(current))!=len(set(keys(current))):raise AssertionError('record completeness/order '+name)
        changed=diff(normalized(baseline),normalized(current));unaffected=0;items=[]
        for x,y in zip(baseline['rows'],current['rows']):
            rowdiff=diff(x,y)
            if not rowdiff:unaffected+=1
            else:items.append({'sku':y['E'],'name':y.get('D'),'authorized_route_change':y['E'] in allowed,
                'old_warehouse':x.get('AU'),'new_warehouse':y.get('AU'),'old_quantity':x.get('_allocated_qty',x.get('AS')),'new_quantity':y.get('_allocated_qty',y.get('AS')),
                'old_amount':x.get('_allocated_amount',x.get('BV')),'new_amount':y.get('_allocated_amount',y.get('BV')),'fields':list(dict.fromkeys(d[0] for d in rowdiff))})
            if name.endswith('/None') and y['E'] not in allowed and rowdiff:failures.append([name,'unaffected unlimited row changed',y['E'],rowdiff])
        changes.append({'case':name,'rows':items,'all_differences':changed})
        reports.append({'case':name,'rows':len(current['rows']),'authorized_route_count':len(eligible.get(name,[])),
             'unchanged_rows':unaffected,'changed_rows':len(items),'old_amount':baseline['summary']['amount'],'new_amount':current['summary']['amount'],
             'old_count':baseline['summary']['positive'],'new_count':current['summary']['positive'],'oracle_differences':len(ds),'record_order_equal':True})
    frozen=json.loads((q/'frozen.json').read_text(encoding='utf-8'));protected={p:sha(Path(p))==h for p,h in frozen['preserved'].items()}
    core={n:sha(ROOT/n)==frozen['core'][str(Path(n))] for n in ['src/model.py','src/one_box_exception.py','src/allocation.py','src/default_config.json','src/default_must_sell.json','src/input_adapter.py','src/parameter_schema.py','src/normal_exports.py']}
    report={'passed':not failures and all(protected.values()) and all(core.values()),'baseline':frozen['baseline'],'cases':reports,'unexpected_differences':failures,'core_unchanged':core,'protected_files':protected,
       'whitelist':['result.created','result.task.pid','result.task.started','result.task.finished','result.version.version','result.version.source_commit','result.version.build_id','result.version.built_at','result.version.routing_version'],
       'comparison':'Exact types, values, list and dictionary order; all rows, baseline model rows, intermediate/audit/settings fields. Baseline oracle substitutes full primary pair only on approved fallback, then runs unchanged old model/allocator. No tolerances.',
       'scope':'Real frozen 14244-row sales + frozen saved catalogs; not current unsaved store. Budget spillover is reported separately from route changes. Old archives tested separately.'}
    js(q/'differences.json',changes);js(q/'report.json',report)
    print(json.dumps({'passed':report['passed'],'cases':len(reports),'unexpected':len(failures)},ensure_ascii=False))
    if not report['passed']:raise AssertionError('See report.json')


if __name__=='__main__':main()
