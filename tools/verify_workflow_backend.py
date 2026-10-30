"""Strict original-versus-candidate business comparison; exports have separate projection oracle."""
import argparse,json,hashlib,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
from parallel_qa import read,clean,diff,WHITELIST

def main():
    p=argparse.ArgumentParser();p.add_argument('--qa',type=Path,required=True);p.add_argument('--old',default='backend_old');p.add_argument('--new',default='backend_new');a=p.parse_args();q=a.qa;old=q/a.old;new=q/a.new;names=json.loads((old/'capture.json').read_text(encoding='utf-8'))['cases'];assert names==json.loads((new/'capture.json').read_text(encoding='utf-8'))['cases'];cases=[]
    for name in names:
        delta=diff(clean(read(old/(name+'.pklz'))),clean(read(new/(name+'.pklz'))));cases.append({'case':name,'differences':len(delta),'details':delta[:50]})
    protected=json.loads((q/'protected_hashes.json').read_text(encoding='utf-8'));changed=[n for n,h in protected.items() if hashlib.sha256((ROOT/n).read_bytes()).hexdigest()!=h]
    inputs=json.loads((q/'inputs_manifest.json').read_text(encoding='utf-8'));changed_inputs=[r['path'] for r in inputs if hashlib.sha256(Path(r['path']).read_bytes()).hexdigest()!=r['sha256']]
    expected={'real_None_True':(430,12268.87),'real_0_True':(0,0),'real_3000_True':(88,2999.87),'real_5000_True':(115,4999.98),'real_10000_True':(390,9980.33),'real_None_False':(422,11890.07)}
    for name,value in expected.items():r=read(new/(name+'.pklz'));assert (r['summary']['positive'],r['summary']['amount'])==value
    report={'passed':not any(c['differences'] for c in cases) and not changed and not changed_inputs,'baseline_commit':'84a0671bd81daa8c2e94a8648c2332b9fc8fa2a9','cases':cases,'whitelist':{k:v for k,v in WHITELIST.items() if k!='Excel'},'strict_values_types_dict_and_list_order':True,'changed_protected':changed,'changed_original_inputs':changed_inputs,'auxiliary_real_totals':expected,'exports':'separate verify_workflow_exports independent literal disabled/stopped projection; no export fields excluded here because these are full business captures'}
    report['candidate_sources']=json.loads((new/'capture.json').read_text(encoding='utf-8'))['source_hashes'];report['capture_directories']=[str(old),str(new)]
    (q/'backend_equivalence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False));assert report['passed']

if __name__=='__main__':main()
