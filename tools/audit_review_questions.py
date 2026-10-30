"""Reproduce external review claims without changing business failure policy."""
import copy,hashlib,importlib.metadata,json,sys,zlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
import model,allocation,input_adapter,storage_chunks,work_repository as wr
out=ROOT/'qa/inbound_091';cases=[]
for q in (1.,1.000001,.999999):
    rows=[dict(E='dust',_candidate_qty=q,_candidate_amount=q,V=1,BD=1,K=0,L=0,H=1,I=7,J=30,AN=1,AM=1,AH=5)]
    try:r=allocation.apply(rows,100);cases.append({'case':'rounding','quantity':q,'result':r})
    except Exception as e:cases.append({'case':'rounding','quantity':q,'error':type(e).__name__+': '+str(e)})
blob=zlib.compress(wr.dumps({'a':1}).encode())
try:storage_chunks.load(blob,expected='0'*64)
except ValueError as e:cases.append({'case':'checksum','optimized':not __debug__,'rejected':True,'error':str(e)})
else:raise RuntimeError('checksum bypassed')
source=dict(E='ratio-zero',H=1.,I=14.,J=14+1e-12,K=0.,L=0.,N=0,O=1.,V=1.,S='启用',AB=.2,AC=.4,AD=.3)
try:model.calculate(source)
except ArithmeticError as e:cases.append({'case':'zero_division_fractional_sales','input':source,'error':type(e).__name__+': '+str(e)})
adapted=input_adapter.execute([(source,{'blocking':[],'export_row':2})],model.Rules())[0]
cases.append({'case':'same_zero_division_through_adapter','quantity':adapted['AS'],'state':adapted['AW'],'blocking':adapted['_meta']['blocking']})
dependencies={}
for line in (ROOT/'requirements-build.txt').read_text().splitlines():
    if '==' in line:
        name,version=line.split('==');dependencies[name]={'pinned':version,'installed':importlib.metadata.version(name)}
report={'cases':cases,'dependencies':dependencies,'python':sys.version,'core_portable_tests':'python -m unittest discover -s tests -v','notes':['assert-free checksum verified also with python -O','existing ratio edge isolated by adapter as calculation error, not healthy zero','noninteger candidate fails store; no business failure-policy change in this release']}
(out/('review_probes_optimized.json' if not __debug__ else 'review_probes.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False))
