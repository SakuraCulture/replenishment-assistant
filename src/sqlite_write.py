"""Cancellable acquisition of the existing SQLite business write lock."""
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
