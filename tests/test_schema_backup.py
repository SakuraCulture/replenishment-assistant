"""A migration backup must include committed WAL data and support a safe retry."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from schema_backup import backup, file_hash


class SQLiteMigrationBackup(unittest.TestCase):
    def test_snapshot_is_standalone_deduplicated_and_does_not_hash_the_live_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'workbench.sqlite3'
            with closing(sqlite3.connect(path, isolation_level=None)) as writer:
                writer.execute('PRAGMA journal_mode=WAL')
                writer.execute('PRAGMA wal_autocheckpoint=0')
                writer.execute('CREATE TABLE orders (id INTEGER PRIMARY KEY)')
                writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                writer.execute('INSERT INTO orders VALUES (17)')
                writer.execute('BEGIN IMMEDIATE')
                writer.execute('INSERT INTO orders VALUES (99)')
                with patch('schema_backup.file_hash', wraps=file_hash) as hashed:
                    first = backup(path, 'before-orders.')
                    self.assertEqual(backup(path, 'before-orders.'), first)
                    self.assertTrue(all(call.args[0] not in (path, Path(str(path)+'-wal')) for call in hashed.call_args_list))
                with closing(sqlite3.connect(first.as_uri()+'?immutable=1', uri=True)) as copied:
                    self.assertEqual(copied.execute('SELECT * FROM orders').fetchall(), [(17,)])
                    self.assertEqual(copied.execute('PRAGMA quick_check').fetchall(), [('ok',)])
                    self.assertEqual(copied.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
                writer.rollback()
                writer.execute('INSERT INTO orders VALUES (23)')
                writer.execute('BEGIN IMMEDIATE')
                self.assertNotEqual(first, backup(path, 'before-orders.'))
                writer.rollback()
            self.assertFalse(list(Path(tmp).glob('*.incomplete*')))

    def test_failed_copy_preserves_completed_backup_and_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'workbench.sqlite3'
            with closing(sqlite3.connect(path, isolation_level=None)) as writer:
                writer.execute('CREATE TABLE orders (id INTEGER PRIMARY KEY)')
                writer.execute('INSERT INTO orders VALUES (1)')
                writer.execute('BEGIN IMMEDIATE')
                good = backup(path, 'before-orders.')
                saved = file_hash(good)
                def interrupt(stage):
                    if getattr(stage, 'total', 0):
                        raise RuntimeError('stop the test copy')
                with self.assertRaisesRegex(RuntimeError, 'stop the test copy'):
                    backup(path, 'before-orders.', interrupt)
                self.assertEqual(file_hash(good), saved)
                self.assertEqual(writer.execute('SELECT * FROM orders').fetchall(), [(1,)])
                self.assertEqual(backup(path, 'before-orders.'), good)
            self.assertFalse(list(Path(tmp).glob('*.incomplete*')))
