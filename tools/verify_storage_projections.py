import sys,json,copy,sqlite3,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tools')]
import work_repository as wr,normal_orders as no,normal_exports as ne,storage_chunks as ch
from structure_oracle import excel_snapshot
out=ROOT/'qa/storage_080/projections';out.mkdir(exist_ok=False)
p=wr.loads((ROOT/'qa/storage_080/normal_stage3/payload.json').read_text(encoding='utf-8'));p['day']=wr.today()
work=wr.Repository(out/'settings');repo=no.Repository(work);s=next(iter(p['results']));result=copy.deepcopy(p['results'][s]);result['baseline']=[{'synthetic_padding':i,'text':'不进入导出但必须保留完整归档'*50} for i in range(4096)]
record={'schema':1,'result':result,'new_focus':p['new_focus'],'work_revision':'fixture','item_ids':{d['E']:str(i) for i,d in enumerate(result['rows'])}}
h=no.fingerprint_owned(result,p['new_focus']);pack=ch.prepare(record,binding=h);oid=str(uuid.uuid4())
with work.connect() as db:
 blob=ch.put(db,pack);db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(p['day'],s,'normal_model',oid,'BH-QA-1',1));db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',(oid,1,h,'fixed',blob))
reads=[];original=ch.Reader.raw
ch.Reader.raw=lambda self,node:(reads.append(node[1]),original(self,node))[1]
full=repo.get(p['day'],s);full_reads=len(reads);reads.clear();projected=repo.get(p['day'],s,view='export');projection_reads=len(reads)
assert projected['payload']['result']=={k:full['payload']['result'][k] for k in ('rows','summary','store','store_name')}
assert projection_reads<full_reads
for name,value in [('full',full),('projection',projected)]:ne.export(value,out/(name+'.xlsx'))
assert excel_snapshot(out/'full.xlsx')==excel_snapshot(out/'projection.xlsx')
# Pin historical revision even when current has advanced.
with work.connect() as db:db.execute('UPDATE normal_orders SET current=2');db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',(oid,2,h,'later',blob))
assert repo.get(p['day'],s,1,view='export')['revision']==1
# Unrelated large blocks are not read by a projection; full read still detects corruption.
manifest=ch.manifest(blob);baseline=next(v for k,v in manifest['tree'][1] if k=='result');baseline=next(v for k,v in baseline[1] if k=='baseline');bad_hash=baseline[1][0][1]
with work.connect() as db:db.execute('UPDATE snapshot_chunks SET data=? WHERE hash=?',(b'corrupt',bad_hash))
assert repo.get(p['day'],s,1,view='export')['payload']==projected['payload']
try:repo.get(p['day'],s,1)
except Exception:pass
else:raise AssertionError('full read skipped corrupted block')
report={'passed':True,'full_block_reads':full_reads,'export_block_reads':projection_reads,'checks':['projection exact fields/values/types','full and projected Excel identical','fixed historical revision','untouched blocks not read','full read rejects damage outside projection'],'integrity_scope':'projection validates manifest and every accessed block; does not claim unrelated block verification'}
(out/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps(report))
