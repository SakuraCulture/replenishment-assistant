"""Hold completed futures in QA only; exercise both completion delivery orders.

Real Windows spawn workers still calculate every product. No rows or result keys
are sorted. Production run/exports are unmodified. This makes the exact same
completion schedule reproducible for baseline and candidate.
"""
import argparse,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
from parallel_qa import clean,diff,read,dump,js,WHITELIST
from structure_oracle import excel_snapshot,source_hashes

def worker(source,qa,out,reverse):
    sys.path[:0]=[str(source/'src'),str(source)]
    os.environ['REPLENISHMENT_DATA_DIR']=str(out/'settings')
    import batch_service as bs,engine as e
    out.mkdir(parents=True,exist_ok=False);inp=qa/'inputs'
    params=json.loads((inp/'parameters.json').read_text(encoding='utf-8'))
    catalogs=json.loads((inp/'catalogs.json').read_text(encoding='utf-8'))
    must=json.loads((inp/'must_sell.json').read_text(encoding='utf-8'))
    loaded=bs.load([inp/'sales.xlsx'],catalogs)
    jobs={s:{'warehouses':e.DEFAULT_WAREHOUSES,'exception':True,'budget':limit,'supply_route':{'primary':p,'secondary':q},'parameter_config':params[c]} for s,p,q,limit,c in [('QA_A','河北仓','山东仓',60,'custom'),('QA_B','山东仓','河北仓',80,'standard')]}
    wait=bs.wait
    def deliver(active,timeout,return_when):
        target=next(reversed(active)) if reverse else next(iter(active))
        done,_=wait([target],timeout=timeout,return_when=return_when)
        return done,set(active)-done
    bs.wait=deliver;events=[]
    results=bs.run(loaded,jobs,must,2,notify=lambda s,state,result,error:events.append((s,state,error)))
    expected=['QA_B','QA_A'] if reverse else ['QA_A','QA_B']
    if list(results)!=expected or [s for s,state,error in events if state=='已完成']!=expected:raise AssertionError('controlled completion order not exercised')
    # Same explicit UI selection order used by the existing export action.
    states={s:{'name':results[s]['store_name'],'state':'已完成','error':''} for s in jobs}
    ordered={s:results[s] for s in states}
    bs.export_batch(ordered,states,out/'batch.xlsx')
    dump(out/'result.pklz',{'events':events,'results':results,'selected_states':states})
    js(out/'capture.json',{'source_hashes':source_hashes(source),'completion_order':list(results),'selected_export_order':list(states),'PYTHONHASHSEED':os.environ.get('PYTHONHASHSEED')})

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--qa',type=Path,required=True);ap.add_argument('--baseline',type=Path,required=True);ap.add_argument('--output',type=Path,required=True);ap.add_argument('--worker',choices=['old','new']);ap.add_argument('--reverse',action='store_true');a=ap.parse_args();out=a.output.resolve();qa=a.qa.resolve()
    if a.worker:return worker(a.baseline.resolve() if a.worker=='old' else ROOT,qa,out,a.reverse)
    out.mkdir(parents=True,exist_ok=False);cases=[];env=os.environ.copy();env.update(PYTHONHASHSEED='0',PYTHONIOENCODING='utf-8')
    for reverse in (False,True):
        folders=[]
        for mode in ('old','new'):
            folder=out/(mode+('-reverse' if reverse else '-forward'));folders.append(folder)
            subprocess.run([sys.executable,__file__,'--qa',str(qa),'--baseline',str(a.baseline.resolve()),'--output',str(folder),'--worker',mode,*(['--reverse'] if reverse else [])],env=env,check=True)
        old,new=folders
        cases.append({'reverse_completion':reverse,'raw_result_differences':diff(clean(read(old/'result.pklz')),clean(read(new/'result.pklz'))),'export_differences':diff(excel_snapshot(old/'batch.xlsx'),excel_snapshot(new/'batch.xlsx'))})
    r={'passed':all(not c['raw_result_differences'] and not c['export_differences'] for c in cases),'cases':cases,'whitelist':WHITELIST,'scope':'Two real processes with QA-only controlled completion delivery; both original store order and reverse. Values/types/events/result dictionary order and full exported cells/styles strictly compared. No production code changed and no product sorting.'}
    js(out/'report.json',r);print(json.dumps(r,ensure_ascii=False))
    if not r['passed']:raise AssertionError('parallel schedule changed business results')
if __name__=='__main__':main()
