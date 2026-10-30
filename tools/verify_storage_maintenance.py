"""Copy-only migration/rollback/cancel tests, with exact immutable record checks."""
import sys,json,sqlite3,hashlib,threading,subprocess,zlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
import storage_maintenance as sm,storage_chunks as ch,work_repository as wr
from runtime_support import Cancelled
import argparse
a=argparse.ArgumentParser();a.add_argument('--output',type=Path,required=True);a.add_argument('--source',type=Path,required=True);args=a.parse_args()
out=args.output;out.mkdir(parents=True,exist_ok=False);source=args.source
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def logical(path):
 with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
  result={}
  for table, in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
   if table.startswith('snapshot_'):continue
   rows=[]
   for record in db.execute('SELECT * FROM '+table+' ORDER BY rowid'):
    record=list(record)
    if table in ('jobs','normal_revisions','normal_work_revisions'):record[-1]=ch.load(record[-1],db)
    rows.append(record)
   result[table]=rows
  return wr.dumps(result)
before=sha(source);expected=logical(source)
legacy=out/'legacy.sqlite3';a=sm.convert(source,legacy,'legacy');assert logical(legacy)==expected
with sqlite3.connect(legacy) as db:
 assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='snapshot_chunks'").fetchone()
 for table in ['jobs','normal_revisions','normal_work_revisions']:
  for blob, in db.execute('SELECT payload FROM '+table):assert isinstance(wr.loads(zlib.decompress(blob)),dict)
upgrade=out/'upgraded.sqlite3';b=sm.convert(legacy,upgrade,'upgrade');assert logical(upgrade)==expected
again=out/'again.sqlite3';sm.convert(upgrade,again,'legacy');assert logical(again)==expected and sha(source)==before
try:sm.convert(source,legacy,'legacy')
except ValueError:pass
else:raise AssertionError('overwrote existing output')
cancel=threading.Event()
def stop(message):
 if str(message).startswith('转换 '):cancel.set()
try:sm.convert(source,out/'cancelled.sqlite3','upgrade',stop,cancel)
except Cancelled:pass
else:raise AssertionError('cancel ignored')
assert not (out/'cancelled.sqlite3').exists() and not list(out.glob('*.tmp')) and sha(source)==before
# Block corruption must fail without publishing a legacy copy.
bad=out/'bad.sqlite3'
with sqlite3.connect(source) as src,sqlite3.connect(bad) as dst:src.backup(dst)
with sqlite3.connect(bad) as db:db.execute("UPDATE snapshot_chunks SET data=? WHERE hash=(SELECT hash FROM snapshot_chunks LIMIT 1)",(b'broken',))
try:sm.convert(bad,out/'bad-output.sqlite3','legacy')
except Exception:pass
else:raise AssertionError('corruption accepted')
assert not (out/'bad-output.sqlite3').exists() and not list(out.glob('*.tmp'))
report={'passed':True,'checks':['source read-only and hash unchanged','all logical SQL rows and decoded values exact after rollback','legacy file contains only old zlib JSON','upgrade roundtrip exact including IDs/revisions/events','exclusive target refuses overwrite','cancel no final target or temporary residue','corrupt input no published output'],'converted_records':a['records']}
(out/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps(report))
