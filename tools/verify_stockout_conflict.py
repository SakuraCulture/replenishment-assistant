import sys,json,sqlite3,zlib,types,hashlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
import model,work_repository as wr
old=types.ModuleType('stockout_baseline');sys.modules[old.__name__]=old
path=ROOT/'qa/storage_080/baseline/src/model.py';exec(compile(path.read_text(encoding='utf-8'),str(path),'exec'),old.__dict__)
output=ROOT/'qa/storage_080/stockout_fix.json';counts={'rows':0,'target':0,'outside_target_differences':0,'target_quantity_changes':0};changes=[]
p=ROOT/'qa/archive_072/snapshot.sqlite3';db=sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True)
for blob, in db.execute('SELECT r.payload FROM normal_orders o JOIN normal_revisions r ON o.oid=r.oid AND o.current=r.revision'):
 r=wr.loads(zlib.decompress(blob))['result'];rules=model.Rules(**r['config']['parameters'])
 for d in r['rows']:
  a=old.calculate(d,rules);b=model.calculate(d,rules);counts['rows']+=1
  target=model.number(d.get('N')) and d['N']>30 and model.number(d.get('J')) and d['J']>0
  if not target:assert wr.dumps(a)==wr.dumps(b),(d['B'],d['E']);continue
  counts['target']+=1;expected=old.calculate({**d,'N':0},rules)
  assert all(wr.dumps(b.get(k))==wr.dumps(expected.get(k)) for k in model.CALCULATED if k not in ('BM','BW'))
  assert b['N']==d['N'];counts['target_quantity_changes']+=a['AS']!=b['AS'];changes.append({'store':d['B'],'sku':d['E'],'raw_N':d['N'],'old':a['AS'],'new':b['AS']})
for n,j in [(30,1),(31,0),(29,0),(0,0)]:
 d=dict(H=0,I=0,J=j,K=0,L=0,N=n,O=1,V=2,S='启用',AB=.5,AC=.5,AD=.3);assert wr.dumps(old.calculate(d))==wr.dumps(model.calculate(d))
d=dict(H=2,I=21,J=40,K=0,L=0,N=31,O=1,V=2,S='启用',AB=.5,AC=.5,AD=.3)
a=old.calculate(d);b=model.calculate(d);assert a['AS']==28 and b['AS']==42
report={'passed':True,'baseline_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'candidate_sha256':hashlib.sha256((ROOT/'src/model.py').read_bytes()).hexdigest(),'counts':counts,'changes':changes,'synthetic':{'old':28,'new':42},'zero_sales_restore_unchanged':True}
output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(counts))
