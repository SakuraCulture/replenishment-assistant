"""Stage 2 storage codec/contracts, isolated SQLite fixtures only."""
import sys,json,sqlite3,copy,zlib,hashlib,tempfile
from pathlib import Path
from datetime import date,datetime
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
import work_repository as wr,storage_chunks as ch
out=ROOT/'qa/storage_080/stage2';out.mkdir(exist_ok=False)
def payload():return {'schema':1,'day':'2026-10-30','model_version':'0.3.0-rules','states':{},'results':{},'input_dirty':True,'store_presets':{'x':{'p':1}},'store_ui':{},'loaded':{'hashes':{'a':'h'},'rows':[{'code':'0000'+str(i),'none':None,'bool':False,'zero':0,'float':0.0,'blank':'','date':date(2026,10,30),'time':datetime(2026,10,30,11,2),'unicode':'试算\r\n=x','list':[i,None]} for i in range(1200)]},'catalogs':{'HB':{'revision':'a','rows':list(range(800))}},'profile':{'revision':'b'}}
p=payload();db=sqlite3.connect(':memory:');prep=ch.prepare(p);expected=wr.dumps(p).encode();assert prep.digest==hashlib.sha256(expected).hexdigest() and prep.size==len(expected)
with db:blob=ch.put(db,prep)
assert wr.dumps(ch.load(blob,db,expected=prep.digest))==wr.dumps(p)
legacy=zlib.compress(expected);assert wr.dumps(ch.load(legacy,db,expected=prep.digest))==wr.dumps(p)
paths=[('store_presets',),('loaded','hashes'),('catalogs','*','revision')]
assert ch.load(blob,db,expected=prep.digest,paths=paths)==ch.load(legacy,db,expected=prep.digest,paths=paths)
repeat=ch.prepare(p,ch.known(db));assert not repeat.chunks and repeat.digest==prep.digest
changed=copy.deepcopy(p);changed['loaded']['rows'][500]['zero']=1;delta=ch.prepare(changed,ch.known(db));assert len(delta.chunks)==1
bad=bytearray(blob);bad[len(ch.MAGIC)]^=1
try:ch.load(bytes(bad),db)
except ValueError:pass
else:raise AssertionError('manifest corruption accepted')
h=next(iter(prep.chunks));db.execute('UPDATE snapshot_chunks SET data=? WHERE hash=?',(zlib.compress(b'bad'),h))
try:ch.load(blob,db)
except ValueError:pass
else:raise AssertionError('block corruption accepted')
work=wr.Repository(out/'legacy');info=wr.summary(p)
with work.connect() as db:db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(p['day'],'original',prep.digest,'stamp',wr.dumps(info),legacy))
assert wr.dumps(work.get_job(p['day'])['payload'])==wr.dumps(p)
r=work.save_job(p['day'],changed,'original');assert wr.dumps(work.get_job(p['day'])['payload'])==wr.dumps(changed)
backups=list(work.path.parent.glob('*.before-chunks-*.sqlite3'));assert len(backups)==1
with sqlite3.connect(backups[0]) as db:assert db.execute('SELECT payload FROM jobs').fetchone()[0]==legacy
assert work.review_job(p['day'])['payload']['catalogs']=={'HB':{'revision':'a'}}
with work.connect() as db:
 before=list(db.execute('SELECT hash FROM snapshot_chunks'));db.execute("CREATE TRIGGER fail_save BEFORE UPDATE ON jobs BEGIN SELECT RAISE(ABORT,'injected'); END")
new=copy.deepcopy(changed);new['loaded']['rows'][0]['zero']=999
try:work.save_job(p['day'],new,r['revision'])
except sqlite3.DatabaseError:pass
else:raise AssertionError('injected transaction accepted')
with work.connect() as db:assert list(db.execute('SELECT hash FROM snapshot_chunks'))==before
assert work.get_job(p['day'])['revision']==r['revision']
try:work.save_job(p['day'],new,'wrong')
except wr.Conflict:pass
else:raise AssertionError('revision mismatch accepted')
report={'passed':True,'canonical_bytes':len(expected),'initial_blocks':len(prep.chunks),'unchanged_new_blocks':len(repeat.chunks),'one_row_change_new_blocks':len(delta.chunks),'cases':['canonical JSON exact','legacy read','full value/type roundtrip','projection parity','dedup','one block changed','manifest corrupt rejected','block corrupt rejected','legacy database backup verified','overwrite revision CAS','failure rollback includes chunks']}
(out/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps(report))
