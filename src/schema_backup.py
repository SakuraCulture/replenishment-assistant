"""Retain one verified pre-migration image for each exact database state."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3

from export_progress import RowProgress


def backup(path, event, progress=lambda _: None):
    """The calling connection owns the database's immediate write reservation."""
    path=Path(path).resolve()
    # Both callers hold BEGIN IMMEDIATE until the migration is committed.
    # Use SQLite to read the live database and its committed WAL pages. On
    # Windows a raw read across the database's lock bytes can fail with EACCES.
    partial = path.with_name(f'.{path.name}.{event}snapshot.incomplete')
    clear_partial(partial)
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as source:
            with closing(sqlite3.connect(partial)) as copy:
                progress('正在备份升级前的数据库')
                source.backup(copy, pages=1024, progress=lambda status, left, total:
                              progress(RowProgress('备份数据库', total - left, total)))
                # Publish one standalone file even when the source is in WAL mode.
                if copy.execute('PRAGMA journal_mode=DELETE').fetchone()[0] != 'delete':
                    raise ValueError('备份未能保存为独立数据库文件')
                progress('正在校验升级前的数据库备份')
                if copy.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise ValueError('备份完整性检查失败')
        # The snapshot is closed and has passed quick_check before we hash it.
        identity = file_hash(partial)
        target = path.with_name(f'{path.stem}.{event}{identity[:32]}.sqlite3')
        if target.is_file():
            if file_hash(target) != identity:
                raise ValueError('已有迁移备份与本次完整快照不符：' + target.name)
            return target
        partial.rename(target)
        return target
    finally:
        clear_partial(partial)


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def clear_partial(path):
    # The writer reservation serializes retries. Only these staging files are
    # disposable after a failed or interrupted backup.
    for suffix in ('-journal', '-wal', '-shm', ''):
        path.with_name(path.name + suffix).unlink(missing_ok=True)
