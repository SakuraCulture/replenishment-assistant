"""Content-addressed original files, independent of immutable calculation payloads."""
import hashlib,sqlite3,io,os,tempfile
from contextlib import closing
from pathlib import Path
import work_repository as wr
from runtime_support import checkpoint

def capture(dbpath,path,expected,progress=lambda _:None,cancel=None):
    path=Path(path);checkpoint(cancel)
    progress('留存原始文件：'+path.name)
    data=path.read_bytes();digest=hashlib.sha256(data).hexdigest()
    if digest!=expected:raise ValueError('原文件在校验后变化，未更新资料：'+path.name)
    checkpoint(cancel)
    with closing(sqlite3.connect(dbpath,timeout=10)) as db,db:
        db.execute('CREATE TABLE IF NOT EXISTS source_files(sha TEXT PRIMARY KEY,name TEXT NOT NULL,saved TEXT NOT NULL,content BLOB NOT NULL)')
        checkpoint(cancel)
        db.execute('INSERT OR IGNORE INTO source_files VALUES(?,?,?,?)',(digest,path.name,wr.now(),data))
    return digest

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
