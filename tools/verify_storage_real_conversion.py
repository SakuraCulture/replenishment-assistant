"""Full legacy real database -> chunks -> legacy; every SQL row compared."""
import argparse,hashlib,json,sqlite3,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
import storage_maintenance as sm,storage_chunks as ch,work_repository as wr

def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def capture(path):
    result={}
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
        names=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for table in names:
            if table in ('snapshot_chunks','snapshot_storage_meta'):continue
            result[table]=[]
            for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid'):
                values=list(row)
                if table in ('jobs','normal_revisions','normal_work_revisions'):
                    payload=ch.load(values[-1],db);values[-1]=hashlib.sha256(wr.dumps(payload).encode()).hexdigest();del payload
                result[table].append(values)
    return result

def main():
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);source=a.source.resolve();before=sha(source)
    receipts=[]
    for mode,src,name in [('upgrade',source,'chunks.sqlite3'),('legacy',out/'chunks.sqlite3','legacy.sqlite3')]:
        print(mode,flush=True);receipts.append(sm.convert(src,out/name,mode))
    old=capture(source)
    for path in [out/'chunks.sqlite3',out/'legacy.sqlite3']:assert wr.dumps(capture(path))==wr.dumps(old)
    assert sha(source)==before
    report={'passed':True,'source_sha256':before,'differences':0,'tables':{k:len(v) for k,v in old.items()},'scope':'every original SQL row in rowid order; full decoded payload canonical hash; IDs/revisions/events/config exact','exclusions':['snapshot_chunks','snapshot_storage_meta'],'receipts':receipts,'source_unchanged':True}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print('full real database roundtrip passed',flush=True)

if __name__=='__main__':main()
