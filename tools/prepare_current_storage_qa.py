"""Build a current-format-only QA copy using the sealed 0.46.0 implementation.

The compatibility codec is loaded exclusively from a Git archive, never shipped.
Original frozen input and all previous deliveries remain untouched.
"""
import hashlib,json,shutil,sqlite3,subprocess,sys,zipfile
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/current_storage_0461'
BASE='51db4bde4b0ed1a5a0576dca838f21ffc8fc1924'
def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def save(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2),'utf-8')
def main():
    QA.mkdir(exist_ok=False);archive=QA/'baseline.zip'
    subprocess.run(['git','archive','-o',str(archive),BASE,'src','tests','tools'],cwd=ROOT,check=True)
    with zipfile.ZipFile(archive) as z:z.extractall(QA/'baseline')
    original=ROOT/'qa/delete_diagnosis_20261103/frozen'
    protected=json.loads((ROOT/'qa/deletion_0452/frozen.json').read_text('utf-8'))['protected']
    for p in [*original.rglob('*'),ROOT/'交付_0.45.2_rc1/补货助手/补货助手.exe',ROOT/'交付_0.46.0_rc2/补货助手/补货助手.exe']:
        if p.is_file():protected[str(p)]=sha(p)
    save(QA/'frozen.json',{'commit':BASE,'archive_sha256':sha(archive),'protected':protected})
    sys.path[:0]=[str(QA/'baseline/src'),str(QA/'baseline/tools')]
    import maintenance_storage as ms,storage_chunks as c,deletion_qa
    fixture=QA/'current-fixture';shutil.copytree(original,fixture)
    path=fixture/'workbench.sqlite3';before=deletion_qa.signature(path);changes=[]
    with closing(sqlite3.connect(path)) as db,db,closing(sqlite3.connect(QA/'conversion-scratch.sqlite3')) as scratch:
        editor=ms.Editor(db,scratch)
        for table in ('normal_revisions','normal_work_revisions','jobs'):
            for rowid, in db.execute(f'SELECT rowid FROM {table}').fetchall():
                raw=db.execute(f'SELECT payload FROM {table} WHERE rowid=?',(rowid,)).fetchone()[0]
                if c.manifest(raw):continue
                tree=editor.legacy_tree(raw);digest,size=editor.checksum(tree)
                if digest!=editor.legacy_digest:raise AssertionError('Canonical content changed')
                binding=db.execute(f'SELECT digest FROM {table} WHERE rowid=?',(rowid,)).fetchone()[0] if table=='normal_revisions' else None
                db.executemany('INSERT OR IGNORE INTO snapshot_chunks VALUES(?,?,?)',scratch.execute('SELECT * FROM snapshot_chunks'))
                blob=c.put(db,c.Prepared(tree,digest,size,{},binding))
                db.execute(f'UPDATE {table} SET payload=? WHERE rowid=?',(blob,rowid))
                changes.append({'table':table,'rowid':rowid,'logical_sha256':digest,'legacy_bytes':len(raw),'manifest_bytes':len(blob)})
                print(json.dumps(changes[-1]),flush=True)
        for table in ('maintenance_chunk_refs','maintenance_legacy_refs'):
            db.execute('DROP TABLE IF EXISTS '+table)
    after=deletion_qa.signature(path)
    changed=[p for p,h in protected.items() if sha(Path(p))!=h]
    report={'passed':before==after and not changed,'records_converted':changes,'before':before,'after':after,'changed_protected':changed,
            'fixture_files':{p.name:sha(p) for p in fixture.rglob('*.sqlite3')},'purpose':'QA-only current-format fixture, semantic signatures preserved; not a product migration'}
    save(QA/'fixture-report.json',report)
    if not report['passed']:raise AssertionError(report)
if __name__=='__main__':main()
