"""Independently reread every surviving snapshot and verify its full byte digest.

This audit runs outside performance timing against isolated completed outputs.
It does not trust the result manifest's declared digest as the business oracle.
"""
import argparse,hashlib,json,sqlite3,sys,time
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
import storage_chunks as c,data_maintenance as m
def main():
    p=argparse.ArgumentParser();p.add_argument('--settings',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    a.settings.resolve().relative_to(ROOT/'qa');a.output.resolve().relative_to(ROOT/'qa')
    if a.output.exists():raise FileExistsError(a.output)
    start=time.perf_counter();seen=set();live=set();counts={};logical_bytes=0;scans=0
    with closing(sqlite3.connect((a.settings/'workbench.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('BEGIN');tables=m.tables(db)
        for table in ('normal_revisions','normal_work_revisions','jobs','scheduled_values'):
            if table not in tables:continue
            counts[table]=0
            for rowid,raw in db.execute(f'SELECT rowid,payload FROM {table}'):
                manifest=c.require_manifest(raw);counts[table]+=1;live.update(m.references(manifest['tree']))
                if table in ('jobs','normal_revisions'):
                    expected=db.execute(f'SELECT digest FROM {table} WHERE rowid=?',(rowid,)).fetchone()[0]
                    assert (manifest['digest'] if table=='jobs' else manifest.get('binding'))==expected,(table,rowid,'index binding')
                key=hashlib.sha256(raw).digest()
                if key in seen:continue
                digest=hashlib.sha256();size=0
                def feed(raw):
                    nonlocal size
                    digest.update(raw);size+=len(raw)
                c.Reader(db).emit(manifest['tree'],feed)
                assert (digest.hexdigest(),size)==(manifest['digest'],manifest['size']),(table,rowid,'full digest')
                seen.add(key);logical_bytes+=size;scans+=1
        actual={h for h, in db.execute('SELECT hash FROM snapshot_chunks')}
        assert actual==live,{'missing':len(live-actual),'orphan':len(actual-live)}
    report={'passed':True,'settings':str(a.settings),'records':counts,'unique_full_snapshot_scans':scans,
            'logical_bytes_verified':logical_bytes,'live_chunks':len(live),'seconds':time.perf_counter()-start,
            'scope':'Read-only; original Reader verifies raw chunk size/hash and complete original JSON stream; exact reachable chunk set; no maintenance-cache shortcut'}
    a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False),flush=True)
if __name__=='__main__':main()
