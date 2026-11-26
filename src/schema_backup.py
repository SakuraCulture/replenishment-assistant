"""Retain one verified pre-migration image for each exact database state."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3

from export_progress import RowProgress


def backup(path, event, progress=lambda _: None):
    """The calling connection owns the database's immediate write reservation."""
    path=Path(path).resolve()
    identity=hashlib.sha256()
    for source in (path,path.with_name(path.name+'-wal')):
        if source.is_file():
            with source.open('rb') as file:
                identity.update(hashlib.file_digest(file,'sha256').digest())
    target=path.with_name(f'{path.stem}.{event}{identity.hexdigest()[:32]}.sqlite3')
    partial=target.with_suffix('.incomplete')
    if target.is_file():
        with closing(sqlite3.connect(target.as_uri()+'?mode=ro',uri=True)) as copy:
            if copy.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                raise ValueError('已有迁移备份未通过完整性校验：'+target.name)
        return target
    # Only this exact pending migration owns this target; a failed previous
    # copy has no restore use. Completed backups never enter this path.
    partial.unlink(missing_ok=True)
    try:
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as source,closing(sqlite3.connect(partial)) as copy:
            source.backup(copy,pages=1024,progress=lambda status,left,total:
                          progress(RowProgress('备份数据库',total-left,total)))
            if copy.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('备份完整性检查失败')
        partial.rename(target)
        return target
    finally:
        partial.unlink(missing_ok=True)
