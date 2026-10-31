"""Compare new captures to this task's immutable 0.16.1 source, never old reports."""
import hashlib,json,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
from parallel_qa import clean,diff,read,WHITELIST
from structure_oracle import excel_snapshot
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def main():
    import argparse
    ap=argparse.ArgumentParser();ap.add_argument('--qa',type=Path,required=True);a=ap.parse_args();q=a.qa.resolve()
    old,new=q/'model_old',q/'model_new';oldcap=json.loads((old/'capture.json').read_text(encoding='utf-8'));newcap=json.loads((new/'capture.json').read_text(encoding='utf-8'));cases=[]
    if oldcap['cases']!=newcap['cases']:raise ValueError('capture cases differ')
    def identity(x):
        if isinstance(x,dict):
            if {'rows','summary','store'}<=x.keys():
                keys=[(r['B'],r['E']) for r in x['rows']]
                if len(keys)!=len(set(keys)) or any(s!=x['store'] for s,k in keys):raise ValueError('nonunique/wrong store records')
            for v in x.values():identity(v)
        elif isinstance(x,(list,tuple)):
            for v in x:identity(v)
    for name in oldcap['cases']:
        x,y=read(old/(name+'.pklz')),read(new/(name+'.pklz'));identity(x);identity(y);d=diff(clean(x),clean(y));cases.append({'case':name,'differences':d})
    for p in old.glob('*.xlsx'):cases.append({'case':p.name,'differences':diff(excel_snapshot(p),excel_snapshot(new/p.name))})
    cores=['model.py','one_box_exception.py','allocation.py','default_config.json','default_must_sell.json','engine.py','input_adapter.py','batch_service.py','warehouse_catalog.py','new_products.py','parameter_schema.py','result_semantics.py','lifecycle_rules.py','normal_exports.py','lifecycle_exports.py','normal_orders.py','normal_delivery.py','storage_chunks.py']
    hashes={n:{'before':sha(q/'baseline/src'/n),'after':sha(ROOT/'src'/n)} for n in cores};count=sum(len(c['differences']) for c in cases)
    report={'passed':not count and all(v['before']==v['after'] for v in hashes.values()),'baseline_commit':'3e167f5c86f64d1d8a98adffda8287b35ab76361','candidate_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),'cases':cases,'difference_count':count,'whitelist':WHITELIST,'core_hashes':hashes,'input_hashes':{str(p):sha(p) for p in (q/'inputs').rglob('*') if p.is_file()},'scope':'14244-row real input; five budgets and exception off; custom parameters; single/dual warehouse and opposite routes; serial/process calculation; conflicts and invalid codes; old mixed master; restore and trial. Exact types/values and list/dict order; no SKU reordering; full metadata except exact dynamic fields.'}
    (q/'model_equivalence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({'passed':report['passed'],'cases':len(cases),'differences':count}))
    if not report['passed']:raise AssertionError('model or export drift')
if __name__=='__main__':main()
