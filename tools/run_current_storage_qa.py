"""Serial before/after measurements on isolated current-format fixtures only."""
import hashlib,json,os,sqlite3,subprocess,sys
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/current_storage_0461'
def run(name,fixture,rounds,parents=1,warm=None):
    cmd=[sys.executable,str(ROOT/'tools/verify_deletion_load.py'),'run','--fixture',str(fixture),'--output',str(QA/name),'--qa-root',str(QA),'--rounds',str(rounds),'--parents',str(parents)]
    if warm:cmd+=['--warm',str(warm)]
    subprocess.run(cmd,cwd=ROOT,check=True,env={**os.environ,'PYTHONIOENCODING':'utf-8','PYTHONHASHSEED':'0'})
def main():
    run('cold',QA/'current-fixture',2)
    # Build complete warm references outside every measured interval. Reuse the
    # first run's survivors; verify any absent/deleted blocks from the untouched input.
    sys.path.insert(0,str(ROOT/'src'));import maintenance_storage as ms
    warm=QA/'warm.sqlite3'
    if warm.exists():raise FileExistsError(warm)
    with closing(sqlite3.connect((QA/'current-fixture/workbench.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(QA/'cold/before-1/settings/workbench.sqlite3')) as old,closing(sqlite3.connect(warm)) as target,target:
        ms.cache_schema(target);target.executemany('INSERT INTO maintenance_chunk_refs VALUES(?,?,?,?)',old.execute('SELECT * FROM maintenance_chunk_refs'))
        for h,size,raw in src.execute('SELECT * FROM snapshot_chunks'):ms.references_for(target,h,size,raw)
    run('warm',QA/'current-fixture',3,warm=warm)
    run('multiple',QA/'current-fixture',2,parents=3,warm=warm)
    run('synthetic-performance',QA/'synthetic',3)
if __name__=='__main__':main()
