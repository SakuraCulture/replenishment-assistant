"""Ordered full-field regression between independently executed source trees."""
import hashlib,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
from parallel_qa import read,clean,diff,WHITELIST
from structure_oracle import excel_snapshot
Q=ROOT/'qa/warehouse_010';a=Q/'regression_old_seed0';b=Q/'regression_new_seed0';sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
ca=json.loads((a/'capture.json').read_text(encoding='utf-8'));cb=json.loads((b/'capture.json').read_text(encoding='utf-8'));assert ca['cases']==cb['cases'];cases=[]
for name in ca['cases']:
    changes=diff(clean(read(a/(name+'.pklz'))),clean(read(b/(name+'.pklz'))));cases.append({'case':name,'differences':len(changes),'details':changes})
for path in a.glob('*.xlsx'):
    changes=diff(excel_snapshot(path),excel_snapshot(b/path.name));cases.append({'case':path.name,'differences':len(changes),'details':changes})
# Every computation/input/routing module and default in this patch remains byte-identical.
core=['model.py','one_box_exception.py','allocation.py','default_config.json','default_must_sell.json','batch_service.py','input_adapter.py','warehouse_catalog.py','engine.py','new_products.py','result_semantics.py','normal_orders.py','storage_chunks.py','work_repository.py','parameter_schema.py','xlsx_exports.py']
hashes={n:{'before':sha(Q/'baseline/src'/n),'after':sha(ROOT/'src'/n)} for n in core};assert all(v['before']==v['after'] for v in hashes.values())
protected=json.loads((Q/'protected.json').read_text(encoding='utf-8'));changed=[p for p,h in protected.items() if not Path(p).is_file() or sha(Path(p))!=h]
r={'passed':all(c['differences']==0 for c in cases) and not changed,'baseline_commit':'6894be792e99f9aa179c1fc5ac8a8e24ffe134b4','strict_order':True,'whitelist':WHITELIST,'cases':cases,'differences':sum(c['differences'] for c in cases),'protected_count':len(protected),'protected_changes':changed,'unchanged_business_files':hashes,'old_capture_sources':ca['source_hashes'],'new_capture_sources':cb['source_hashes']}
(Q/'regression_final.json').write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({'passed':r['passed'],'cases':len(cases),'differences':r['differences'],'protected_changes':len(changed)}));assert r['passed']
