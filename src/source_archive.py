"""Content-addressed original files, independent of immutable calculation payloads."""
import hashlib,sqlite3,io,os,tempfile
from contextlib import closing
from pathlib import Path
import work_repository as wr
from runtime_support import checkpoint

def capture(dbpath,path,expected,progress=lambda _:None,cancel=None):
    from portable_assets import resolve
    path=Path(path);checkpoint(cancel)
    progress('留存原始文件：'+path.name)
    with resolve(path).open('rb') as source,closing(sqlite3.connect(dbpath,timeout=10)) as db,db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS source_files(sha TEXT PRIMARY KEY,name TEXT NOT NULL,saved TEXT NOT NULL,content BLOB NOT NULL)')
        size=os.fstat(source.fileno()).st_size
        exists=db.execute('SELECT 1 FROM source_files WHERE sha=?',(expected,)).fetchone()
        digest=hashlib.sha256()
        def copy(target=None):
            for data in iter(lambda:source.read(1024**2),b''):
                checkpoint(cancel);digest.update(data)
                if target is not None:target.write(data)
        if exists:
            copy()
        else:
            row=db.execute('INSERT INTO source_files VALUES(?,?,?,zeroblob(?))',(expected,path.name,wr.now(),size)).lastrowid
            with db.blobopen('source_files','content',row) as blob:copy(blob)
        if digest.hexdigest()!=expected:raise ValueError('原文件在校验后变化，未更新资料：'+path.name)
        checkpoint(cancel)
    return expected

def read(db,digest):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='source_files'").fetchone():return None
    row=db.execute('SELECT name,content FROM source_files WHERE sha=?',(digest,)).fetchone()
    if row and hashlib.sha256(row[1]).hexdigest()!=digest:raise ValueError('原文件快照校验失败')
    return row

def export(db,digest,path,cancel=None):
    row=read(db,digest)
    if not row:raise ValueError('历史原文件未留存')
    target=Path(path);fd,tmp=tempfile.mkstemp(dir=target.parent,prefix='.source-')
    try:
        with os.fdopen(fd,'wb') as f:f.write(row[1])
        checkpoint(cancel);os.link(tmp,target)
    finally:Path(tmp).unlink(missing_ok=True)
    return str(target)

def retained_import(fn,args,result,dbpath,progress,cancel):
    """Called in the existing import worker after validation, before UI adoption."""
    import batch_service,warehouse_catalog,new_product_focus,priority_list
    files={}
    if fn is batch_service.load:files=(result.get('intake') or {}).get('hashes',{})
    elif fn is warehouse_catalog.import_catalog:files={result['source_path']:result['source_sha256']}
    elif fn is new_product_focus.import_list:files={result['source_path']:result['source_sha256']}
    elif fn is priority_list.import_profile:
        # JSON backup carries the old Excel hash, so verify against its own bytes.
        p=Path(args[0]);files={str(p):hashlib.sha256(p.read_bytes()).hexdigest() if p.suffix.lower()=='.json' else result['source_sha256']}
    for path,digest in files.items():capture(dbpath,path,digest,progress,cancel)
