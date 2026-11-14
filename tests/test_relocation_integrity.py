"""SQLite relocation validation uses disposable copies, never real application data."""
import hashlib
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname
from unittest.mock import patch

import relocation_integrity as integrity


JOURNAL_MAGIC = bytes.fromhex('d9d505f920a163d7')
HOT_JOURNAL_SCRIPT = r'''
import os, sqlite3, sys
from pathlib import Path
path = Path(sys.argv[1])
if any(Path(str(path) + suffix).exists() for suffix in ('', '-journal', '-wal', '-shm')):
    raise SystemExit('Synthetic fixture destination must be completely new')
db = sqlite3.connect(path)
db.execute('PRAGMA journal_mode=DELETE')
db.execute('PRAGMA synchronous=FULL')
db.execute('PRAGMA cache_size=4')
db.execute('CREATE TABLE payload(id INTEGER PRIMARY KEY, value TEXT NOT NULL)')
db.executemany('INSERT INTO payload VALUES(?, ?)', [(n, 'committed-' + 'a' * 8192) for n in range(96)])
db.commit()
db.execute('BEGIN IMMEDIATE')
db.execute("UPDATE payload SET value = 'uncommitted-' || substr(value, 1, 8192)")
# The small page cache spills this uncommitted transaction to disk. Leave its
# genuine rollback journal to emulate a crash in our own synthetic process.
os._exit(73)
'''


def seed_hot_journal(path):
    """Create only the caller's new synthetic DB and its SQLite sidecars."""
    path = Path(path).absolute()
    if path.is_symlink() or path.is_junction():
        raise ValueError('Synthetic destination must not be a link')
    if any(Path(str(path) + suffix).exists() for suffix in ('', '-journal', '-wal', '-shm')):
        raise ValueError('Synthetic fixture destination must be completely new')
    path.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
    result = subprocess.run([sys.executable, '-I', '-c', HOT_JOURNAL_SCRIPT, str(path)],
                            env=env, capture_output=True, timeout=30)
    if result.returncode != 73:
        raise AssertionError('Synthetic crash fixture failed: ' + result.stderr.decode('utf-8', errors='replace'))
    journal = Path(str(path) + '-journal')
    if not path.is_file() or not journal.is_file() or journal.stat().st_size <= 512:
        raise AssertionError('Synthetic fixture did not retain a hot journal')
    with journal.open('rb') as stream:
        if stream.read(8) != JOURNAL_MAGIC:
            raise AssertionError('Synthetic journal has no hot-journal header')
        stream.seek(-8, os.SEEK_END)
        if stream.read(8) == JOURNAL_MAGIC:
            raise AssertionError('Single-DB fixture unexpectedly has a super-journal trailer')
    return path


def fingerprint(folder):
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path(folder).rglob('*') if p.is_file()}


class RelocationIntegrity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relocation-integrity-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.scratch = self.root / 'scratch'
        self.source.mkdir()
        self.scratch.mkdir()

    def ordinary(self, name='ordinary.sqlite3'):
        path = self.source / name
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE facts(id INTEGER PRIMARY KEY, label TEXT)')
            db.execute("INSERT INTO facts VALUES(1, 'committed')")
        return path

    def assert_scratch_empty(self):
        self.assertEqual(list(self.scratch.iterdir()), [], 'Owned validation scratch was not removed')

    def test_ordinary_database_is_verified_without_changing_its_bytes(self):
        path = self.ordinary()
        before = fingerprint(self.source)
        ticks = []
        integrity.verify_database(path, 'settings/ordinary.sqlite3', self.scratch, lambda: ticks.append(True))
        self.assertEqual(fingerprint(self.source), before)
        self.assertTrue(ticks, 'Database validation must provide progress/heartbeat')
        self.assert_scratch_empty()

    def test_real_hot_journal_reproduces_776_then_verifies_without_mutating_original(self):
        path = seed_hot_journal(self.source / 'ExtensionActivityEdge')
        before = fingerprint(self.source)
        # This read-only connection touches only the freshly generated fixture.
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        try:
            with self.assertRaises(sqlite3.OperationalError) as caught:
                db.execute('PRAGMA integrity_check').fetchall()
            self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_READONLY_ROLLBACK)
            self.assertEqual(caught.exception.sqlite_errorname, 'SQLITE_READONLY_ROLLBACK')
        finally:
            db.close()
        self.assertEqual(fingerprint(self.source), before)
        integrity.verify_database(path, 'settings/browsers/hb/ExtensionActivityEdge', self.scratch)
        self.assertEqual(fingerprint(self.source), before)
        self.assert_scratch_empty()

    def test_dirty_wal_latest_commit_is_read_from_owned_clone_without_original_shm(self):
        path = self.ordinary('dirty-wal.sqlite3')
        writer = sqlite3.connect(path)
        self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        writer.execute("UPDATE facts SET label='latest-committed-in-wal'")
        writer.commit()
        wal = Path(str(path) + '-wal')
        self.assertGreater(wal.stat().st_size, 32)
        # Simulate the relocation's byte-exact DB+WAL material, with SHM omitted.
        material = self.source / 'material'
        material.mkdir()
        original = material / path.name
        original.write_bytes(path.read_bytes())
        Path(str(original) + '-wal').write_bytes(wal.read_bytes())
        self.assertFalse(Path(str(original) + '-shm').exists())
        before = fingerprint(self.source)
        connect = sqlite3.connect
        observed = []

        def observe_clone(database, *args, **kwargs):
            value = os.fspath(database)
            candidate = Path(url2pathname(unquote(urlsplit(value).path))) if value.startswith('file:') else Path(value)
            self.assertTrue(candidate.resolve().is_relative_to(self.scratch.resolve()),
                            'Verifier must open only its disposable clone')
            db = connect(database, *args, **kwargs)
            try:
                observed.append(db.execute('SELECT label FROM facts WHERE id=1').fetchone()[0])
            except BaseException:
                db.close()
                raise
            return db

        with patch.object(integrity.sqlite3, 'connect', side_effect=observe_clone):
            integrity.verify_database(original, 'settings/analysis/dirty-wal.sqlite3', self.scratch)
        self.assertEqual(observed, ['latest-committed-in-wal'])
        self.assertEqual(fingerprint(self.source), before)
        self.assertFalse(Path(str(original) + '-shm').exists())
        self.assert_scratch_empty()

    def test_corrupt_database_is_rejected_with_its_relative_name(self):
        path = self.ordinary('corrupt.sqlite3')
        data = bytearray(path.read_bytes())
        data[100] = 0x7f  # Invalid page-1 b-tree type; retain SQLite magic.
        path.write_bytes(data)
        before = fingerprint(self.source)
        relative = 'settings/history/corrupt.sqlite3'
        with self.assertRaises(Exception) as caught:
            integrity.verify_database(path, relative, self.scratch)
        self.assertIn(relative, str(caught.exception))
        self.assertEqual(fingerprint(self.source), before)
        self.assert_scratch_empty()

    def test_super_journal_reference_is_rejected_and_external_sentinel_is_untouched(self):
        path = seed_hot_journal(self.source / 'attached.sqlite3')
        journal = Path(str(path) + '-journal')
        sentinel = self.root / 'outside-mj1234569ab'
        sentinel.write_bytes(b'outside-owned-validator-scratch-must-remain-identical')
        name = str(sentinel).encode('utf-8')
        checksum = sum(b if b < 128 else b - 256 for b in name) & 0xffffffff
        page_size = int.from_bytes(path.read_bytes()[16:18], 'big')
        page_size = 65536 if page_size == 1 else page_size
        trailer = ((0x40000000 // page_size + 1).to_bytes(4, 'big') + name +
                   len(name).to_bytes(4, 'big') + checksum.to_bytes(4, 'big') + JOURNAL_MAGIC)
        with journal.open('ab') as stream:
            stream.write(trailer)
        before = fingerprint(self.source)
        sentinel_before = sentinel.read_bytes()
        relative = 'settings/backups/attached.sqlite3'
        with self.assertRaises(Exception) as caught:
            integrity.verify_database(path, relative, self.scratch)
        self.assertIn(relative, str(caught.exception))
        self.assertEqual(sentinel.read_bytes(), sentinel_before)
        self.assertEqual(fingerprint(self.source), before)
        self.assert_scratch_empty()

    def test_progress_exception_preserves_the_original_exception_and_cleans_scratch(self):
        path = self.ordinary()
        before = fingerprint(self.source)
        original = RuntimeError('synthetic-heartbeat-first-error')

        def fail_tick():
            raise original

        with self.assertRaises(RuntimeError) as caught:
            integrity.verify_database(path, 'settings/ordinary.sqlite3', self.scratch, fail_tick)
        self.assertIs(caught.exception, original)
        self.assertEqual(fingerprint(self.source), before)
        self.assert_scratch_empty()

    def test_sqlite_progress_callback_error_is_not_replaced_by_interrupted(self):
        path = self.ordinary()
        before = fingerprint(self.source)
        original = RuntimeError('synthetic-native-sqlite-heartbeat-error')
        native_callback = [False]
        observed = []
        connect = sqlite3.connect

        class NativeProgressConnection(sqlite3.Connection):
            def set_progress_handler(self, callback, steps=1000):
                def inside_sqlite():
                    native_callback[0] = True
                    observed.append(True)
                    try:
                        return callback()
                    finally:
                        native_callback[0] = False
                # Use the real native VM progress hook even for a tiny fixture.
                super().set_progress_handler(inside_sqlite, 1)

        def observed_connect(database, *args, **kwargs):
            return connect(database, *args, factory=NativeProgressConnection, **kwargs)

        def fail_only_in_native_callback():
            if native_callback[0]:
                raise original

        with patch.object(integrity.sqlite3, 'connect', side_effect=observed_connect):
            with self.assertRaises(RuntimeError) as caught:
                integrity.verify_database(path, 'settings/native-progress.sqlite3', self.scratch,
                                          fail_only_in_native_callback)
        self.assertTrue(observed, 'The real SQLite VM progress handler was not exercised')
        self.assertIs(caught.exception, original)
        self.assertEqual(fingerprint(self.source), before)
        self.assert_scratch_empty()

    def test_insufficient_space_fails_before_creating_a_clone_and_names_the_database(self):
        path = self.ordinary()
        before = fingerprint(self.source)
        relative = 'settings/space-check/ordinary.sqlite3'
        with patch.object(integrity.shutil, 'disk_usage', return_value=SimpleNamespace(free=0)), \
                patch.object(integrity.tempfile, 'mkdtemp', side_effect=AssertionError('No clone before space check')):
            with self.assertRaises(ValueError) as caught:
                integrity.verify_database(path, relative, self.scratch)
        self.assertIn(relative, str(caught.exception))
        self.assertEqual(fingerprint(self.source), before)
        self.assert_scratch_empty()

    def test_cleanup_failure_cannot_be_reported_as_success(self):
        path = self.ordinary()
        before = fingerprint(self.source)
        relative = 'settings/cleanup-check/ordinary.sqlite3'
        failure = PermissionError('synthetic-owned-scratch-cleanup-denied')
        with patch.object(integrity, '_remove_scratch', side_effect=failure):
            with self.assertRaises(ValueError) as caught:
                integrity.verify_database(path, relative, self.scratch)
        self.assertIn(relative, str(caught.exception))
        self.assertIn('synthetic-owned-scratch-cleanup-denied', str(caught.exception))
        self.assertEqual(fingerprint(self.source), before)
        self.assertTrue(list(self.scratch.iterdir()), 'Failed cleanup must retain its owned diagnostic clone')

    def test_first_progress_error_is_not_hidden_by_a_second_cleanup_error(self):
        path = self.ordinary()
        before = fingerprint(self.source)
        relative = 'settings/cleanup-check/ordinary.sqlite3'
        original = RuntimeError('synthetic-first-copy-heartbeat-error')
        calls = []

        def fail_after_clone_created():
            calls.append(True)
            if len(calls) >= 2:
                raise original

        with patch.object(integrity, '_remove_scratch', side_effect=PermissionError('synthetic-second-cleanup-error')):
            with self.assertRaises(RuntimeError) as caught:
                integrity.verify_database(path, relative, self.scratch, fail_after_clone_created)
        self.assertIs(caught.exception, original)
        notes = '\n'.join(getattr(caught.exception, '__notes__', []))
        self.assertIn('synthetic-second-cleanup-error', notes)
        self.assertIn(relative, notes)
        self.assertEqual(fingerprint(self.source), before)
        self.assertTrue(list(self.scratch.iterdir()))


if __name__ == '__main__':
    unittest.main()
