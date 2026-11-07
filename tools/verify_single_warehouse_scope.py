"""Compare single-warehouse scope against a frozen engine; synthetic QA only.

--baseline must contain src/ from the previous commit. --output must be new.
Workers write only their output directory and never connect to a platform.
"""
import argparse,copy,json,os,subprocess,sys
from pathlib import Path
from parallel_qa import clean,diff,dump,read,js,sha

ROOT=Path(__file__).resolve().parents[1]


def worker(source,inputs,out,oracle=False):
    sys.path.insert(0,str(source/'src'))
    import batch_service as bs,warehouse_catalog as wc
    out.mkdir(parents=True,exist_ok=False)
    os.environ['REPLENISHMENT_DATA_DIR']=str(out/'settings')
    catalogs=json.loads((inputs/'catalogs.json').read_text('utf-8'))
    profile=json.loads((source/'src/default_must_sell.json').read_text('utf-8'))
    loaded=bs.load([inputs/'sales.xlsx'],catalogs)
    results={}
    for store in loaded['stores']:
        for primary,other in [('山东仓','河北仓'),('河北仓','山东仓')]:
            for secondary in [None,other]:
                selected=copy.deepcopy(loaded)
                # Independent old-engine oracle: hide the unused warehouse
                # from routing, retaining configuration and source provenance.
                if oracle and secondary is None:
                    selected['warehouse_rows']={primary:selected['warehouse_rows'][primary]}
                for budget in [None,0,60]:
                    for exception in [True,False]:
                        settings={'supply_route':{'primary':primary,'secondary':secondary},
                                  'warehouses':{'河北仓':[9,4],'山东仓':[13,7]},
                                  'exception':exception,'budget':budget}
                        result=bs.calculate_store(selected,store,settings,profile)
                        if oracle and secondary is None:
                            for row in [*result['rows'],*result['baseline']]:
                                if row['_source']=='地采商品':
                                    row['_meta']['routing'].update(reason=f'本次仅由{primary}供货，该仓无此商品，按本次地采处理',version='0.5.2')
                        name=f'{store}/{primary}/{secondary}/{budget}/{exception}'
                        results[name]=result
    dump(out/'results.pklz',results)


def normalized(result):
    result=clean(result)
    result['version']['routing_version']='<application routing capability>'
    return result


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--baseline',type=Path);ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--worker',type=Path);ap.add_argument('--inputs',type=Path)
    ap.add_argument('--oracle',action='store_true');a=ap.parse_args()
    if a.worker:return worker(a.worker.resolve(),a.inputs.resolve(),a.output.resolve(),a.oracle)
    if not a.baseline or not (a.baseline/'src/warehouse_catalog.py').is_file():raise ValueError('Frozen baseline src/ required')
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);inputs=out/'inputs';inputs.mkdir()
    sys.path[:0]=[str(ROOT),str(ROOT/'src')]
    from verify_warehouse import fixtures
    import openpyxl,warehouse_catalog as wc
    fixtures(inputs)
    # The original routing fixture predates the explicit admission tag field.
    # A present, blank tag column means no tags; a missing column is blocked.
    for warehouse in wc.WAREHOUSES:
        path=inputs/(warehouse+'.xlsx');book=openpyxl.load_workbook(path)
        book.active.cell(1,book.active.max_column+1,'商品标签')
        book.save(path);book.close()
    book=openpyxl.load_workbook(inputs/'河北仓.xlsx');sheet=book.active
    sheet.append([c.value for c in sheet[2]]);book.save(inputs/'duplicate.xlsx');book.close()
    js(inputs/'catalogs.json',{w:wc.import_catalog(inputs/(w+'.xlsx'),w) for w in wc.WAREHOUSES})
    before={p.name:sha(p) for p in inputs.iterdir() if p.is_file()}
    for name,source,oracle in [('old',a.baseline.resolve(),False),('oracle',a.baseline.resolve(),True),('new',ROOT,False)]:
        command=[sys.executable,__file__,'--worker',str(source),'--inputs',str(inputs),'--output',str(out/name)]
        if oracle:command.append('--oracle')
        with (out/(name+'.log')).open('w',encoding='utf-8') as log:
            subprocess.run(command,stdout=log,stderr=log,check=True)
    old=read(out/'old/results.pklz');expected=read(out/'oracle/results.pklz');new=read(out/'new/results.pklz')
    reports=[];failures=[]
    for name,result in new.items():
        baseline=old[name];differences=diff(normalized(expected[name]),normalized(result))
        failures.extend([[name,*d] for d in differences])
        changed=[d['E'] for d,n in zip(baseline['rows'],result['rows']) if diff(d,n)]
        route=result['supply_route'];single=route['secondary'] is None
        local={d['E'] for d in result['rows'] if d['_source']=='地采商品'}
        if single and not set(changed)<=local:failures.append([name,'changed non-local rows',changed])
        if not single and diff(normalized(baseline),normalized(result)):failures.append([name,'dual warehouse changed'])
        if [d['E'] for d in baseline['rows']]!=[d['E'] for d in result['rows']]:failures.append([name,'row identities/order changed'])
        # In this fixture exactly one SKU exists only in the unused warehouse.
        if single and not (result['summary']['issues']==baseline['summary']['issues']-1
                           and result['summary']['local']==baseline['summary']['local']+1):
            failures.append([name,'review/local count mismatch'])
        if baseline['summary']['amount']!=result['summary']['amount']:failures.append([name,'purchase amount changed'])
        reports.append({'case':name,'rows':len(result['rows']),'single_warehouse':single,
                        'old_issues':baseline['summary']['issues'],'new_issues':result['summary']['issues'],
                        'old_local':baseline['summary']['local'],'new_local':result['summary']['local'],
                        'old_amount':baseline['summary']['amount'],'new_amount':result['summary']['amount'],
                        'changed_skus':changed,'oracle_differences':len(differences)})
    after={p.name:sha(p) for p in inputs.iterdir() if p.is_file()}
    if before!=after:failures.append(['input files changed'])
    report={'passed':not failures,'cases':reports,'unexpected_differences':failures,'input_hashes':after,
            'comparison':'Exact type, value, row and dictionary order. Old engine oracle scopes only the routing warehouse rows; approved single-local audit reason/version are explicit. No model or budget changes.',
            'ignored_fields':['result.created','result.task.pid/started/finished','result.version.version/source_commit/build_id/built_at/routing_version'],
            'scope':'Synthetic two-store, both single/dual directions, 3 budgets and both one-box settings. No production data or platform calls.'}
    js(out/'report.json',report)
    print(json.dumps({'passed':report['passed'],'cases':len(reports),'unexpected':len(failures)},ensure_ascii=False))
    if failures:raise AssertionError('See report.json')


if __name__=='__main__':main()
