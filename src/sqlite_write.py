"""Cancellable waits at SQLite business transaction boundaries."""
import sqlite3
import time

from runtime_support import checkpoint


def begin(db, cancel,*,deadline=None):
    start = time.monotonic()
    end=min(start+10,deadline) if deadline is not None else start+10
    while True:
        checkpoint(cancel)
        try: db.execute('BEGIN IMMEDIATE'); checkpoint(cancel); return
        except sqlite3.OperationalError as exc:
            if 'locked' not in str(exc).lower() or time.monotonic() > end: raise


def commit(db, cancel):
    """Retry a busy COMMIT on the same transaction without replaying writes."""
    deadline = time.monotonic() + 10
    while True:
        checkpoint(cancel)
        try:
            db.commit()
            return
        except sqlite3.OperationalError as exc:
            if getattr(exc, 'sqlite_errorcode', 0) & 255 != sqlite3.SQLITE_BUSY or not db.in_transaction:
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError('本地资料仍在读取，本次核对结果尚未保存。请稍后重试。') from exc
            time.sleep(.025)
