"""Current-format backup/integrity/cancel tests, with exact immutable record checks."""
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
  # Original-file snapshots are SQLite BLOBs too. Preserve their bytes in the
  # comparison instead of omitting the newer source_files table.
  def encode(value):
   if isinstance(value,bytes):return {'__qa_binary_hex__':value.hex()}
   if isinstance(value,list):return [encode(v) for v in value]
   if isinstance(value,dict):return {k:encode(v) for k,v in value.items()}
   return value
  return wr.dumps(encode(result))
before=sha(source);expected=logical(source)
target=out/'backup.sqlite3';a=sm.backup(source,target);assert logical(target)==expected and sha(source)==before
try:sm.backup(source,target)
except ValueError:pass
else:raise AssertionError('overwrote existing output')
cancel=threading.Event()
def stop(message):
 if str(message).startswith('校验备份 '):cancel.set()
try:sm.backup(source,out/'cancelled.sqlite3',stop,cancel)
except Cancelled:pass
else:raise AssertionError('cancel ignored')
assert not (out/'cancelled.sqlite3').exists() and not list(out.glob('*.tmp')) and sha(source)==before
# Block corruption must fail without publishing a backup.
bad=out/'bad.sqlite3'
with sqlite3.connect(source) as src,sqlite3.connect(bad) as dst:src.backup(dst)
with sqlite3.connect(bad) as db:db.execute("UPDATE snapshot_chunks SET data=? WHERE hash=(SELECT hash FROM snapshot_chunks LIMIT 1)",(b'broken',))
try:sm.backup(bad,out/'bad-output.sqlite3')
except Exception:pass
else:raise AssertionError('corruption accepted')
assert not (out/'bad-output.sqlite3').exists() and not list(out.glob('*.tmp'))
report={'passed':True,'checks':['source read-only and hash unchanged','all SQL rows and decoded values exact in backup','exclusive target refuses overwrite','cancel no final target or temporary residue','corrupt input no published output'],'copied_records':a['records']}
(out/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps(report))
