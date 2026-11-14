"""Validate SQLite in a disposable copy, preserving transferred DB/journal bytes."""
import hashlib,os,shutil,sqlite3,tempfile
from contextlib import closing
from pathlib import Path

JOURNAL_MAGIC=bytes.fromhex('d9d505f920a163d7')
CHUNK=1024*1024

def _require_space(parent,needed):
    if shutil.disk_usage(parent).free<needed:
        raise ValueError(f'数据库独立校验空间不足，至少需要约{needed/1024**2:.1f} MiB；未切换资料位置')

def _remove_scratch(folder,parent):
    # Only the exact fresh directory we created may be removed recursively.
    if folder.is_symlink() or folder.is_junction() or folder.resolve().parent!=parent.resolve() or not folder.name.startswith('.sqlite-check-'):
        raise ValueError('独立校验目录身份变化，保留临时副本，未清理：'+str(folder))
    shutil.rmtree(folder)

def _hash(path,tick):
    value=hashlib.sha256()
    with path.open('rb') as file:
        while block:=file.read(CHUNK):value.update(block);tick()
    return value.digest()

def _copy_file(source,target,tick):
    value=hashlib.sha256()
    with source.open('rb') as original,target.open('xb') as copy:
        while block:=original.read(CHUNK):copy.write(block);value.update(block);tick()
        copy.flush();os.fsync(copy.fileno())
    if _hash(target,tick)!=value.digest() or _hash(source,tick)!=value.digest():
        raise ValueError('数据库校验材料在复制期间变化：'+str(source))

def verify_database(file,relative_name,scratch_parent,tick=lambda:None):
    """Open only our own rw clone; never recover or checkpoint the transfer copy."""
    file=Path(file);parent=Path(scratch_parent).resolve();scratch=None;failure=None
    materials=[('',file)]
    for suffix in ('-wal','-journal'):
        side=Path(str(file)+suffix)
        if side.exists():materials.append((suffix,side))
    for _,path in materials:
        if path.is_symlink() or not path.is_file():
            raise ValueError('数据库校验材料不是普通文件：'+relative_name)
    try:
        # Recovery can grow the clone; retain space for copied sidecars, their
        # replay, and SHM. The main relocation archive reserve remains separate.
        needed=file.stat().st_size+2*sum(p.stat().st_size for suffix,p in materials if suffix)+64*1024**2
        try:_require_space(parent,needed)
        except ValueError as exc:raise ValueError(relative_name+'；'+str(exc)) from exc
        tick()
        scratch=Path(tempfile.mkdtemp(prefix='.sqlite-check-',dir=parent))
        clone=scratch/'database.sqlite'
        for suffix,path in materials:_copy_file(path,Path(str(clone)+suffix),tick)
        journal=Path(str(clone)+'-journal')
        if journal.exists() and journal.stat().st_size>=8:
            with journal.open('rb') as handle:handle.seek(-8,2);tail=handle.read(8)
            # Recovery follows super-journal paths embedded in this trailer.
            # Those may be absolute and outside our sandbox; never follow them.
            if tail==JOURNAL_MAGIC:
                raise ValueError('数据库日志包含跨库事务引用，未在原位置恢复；请保留材料核对：'+relative_name)
        callback_errors=[]
        def heartbeat():
            try:tick();return 0
            except BaseException as exc:callback_errors.append(exc);return 1
        try:
            with closing(sqlite3.connect(clone.as_uri()+'?mode=rw',uri=True)) as db:
                db.set_progress_handler(heartbeat,10000)
                rows=db.execute('PRAGMA integrity_check').fetchall()
                if rows!=[('ok',)]:raise ValueError('数据库完整性校验失败：'+relative_name+'；'+str(rows[:3]))
        except sqlite3.Error:
            if callback_errors:raise callback_errors[0]
            raise
        tick()
    except sqlite3.Error as exc:
        detail=f'{getattr(exc,"sqlite_errorname","SQLITE_ERROR")} / {getattr(exc,"sqlite_errorcode","?")}'
        failure=ValueError('数据库独立副本校验失败：'+relative_name+'；'+detail+'；'+str(exc));failure.__cause__=exc
    except BaseException as exc:failure=exc
    if scratch is not None:
        try:_remove_scratch(scratch,parent)
        except BaseException as exc:
            message='数据库独立校验临时副本未清理：'+relative_name+'；'+str(scratch)+'；'+str(exc)
            if failure is not None:failure.add_note(message)
            else:failure=ValueError(message);failure.__cause__=exc
    if failure is not None:raise failure
