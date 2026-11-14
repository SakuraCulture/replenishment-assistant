"""Retired snapshots fail closed; current snapshots keep integrity and atomicity."""
import hashlib,sqlite3,sys,tempfile,threading,unittest,zlib
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import storage_chunks as c,storage_maintenance as backup,maintenance_storage as ms
import data_maintenance as m,work_repository as wr
import test_data_maintenance as fixtures
from runtime_support import Cancelled

class FormatContract(unittest.TestCase):
    def test_old_load_and_edit_refused_without_decompression(self):
        old=zlib.compress(b'{"value": 1}')
        with closing(sqlite3.connect(':memory:')) as db,closing(sqlite3.connect(':memory:')) as scratch:
            editor=ms.Editor(db,scratch)
            with patch.object(c.zlib,'decompress',side_effect=AssertionError('old payload decoded')):
                for operation in (lambda:c.load(old,db),lambda:c.load(old,db,paths=[('value',)]),lambda:editor.rewrite(old,{'T'})):
                    with self.assertRaisesRegex(ValueError,'已停用'):operation()
    def test_current_digest_and_binding_still_checked(self):
        with closing(sqlite3.connect(':memory:')) as db:
            blob=c.put(db,c.prepare({'value':1},binding='signed'))
            with self.assertRaisesRegex(ValueError,'不一致'):c.load(blob,db,expected='wrong')
            with self.assertRaisesRegex(ValueError,'不一致'):c.load(blob,db,binding='wrong')
            db.execute('UPDATE snapshot_chunks SET size=size+1')
            with self.assertRaisesRegex(ValueError,'校验'):c.load(blob,db)
    def test_all_snapshot_owners_are_checked_without_full_payload_select(self):
        for table in ('jobs','normal_revisions','normal_work_revisions','scheduled_values'):
            with self.subTest(table=table),closing(sqlite3.connect(':memory:')) as db:
                db.execute(f'CREATE TABLE {table}(payload BLOB)');blob=c.put(db,c.prepare({'safe':True}))
                db.execute(f'INSERT INTO {table} VALUES(?)',(blob,));c.require_current(db)
                db.execute(f'INSERT INTO {table} VALUES(?)',(zlib.compress(b'{}'),))
                with self.assertRaisesRegex(ValueError,'已停用'):c.require_current(db)
                self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0],2)
    def test_format_check_can_cancel(self):
        with closing(sqlite3.connect(':memory:')) as db:
            db.execute('CREATE TABLE jobs(payload BLOB)');db.execute('INSERT INTO jobs VALUES(?)',(c.MAGIC,))
            event=threading.Event();event.set()
            with self.assertRaises(Cancelled):c.require_current(db,event)

class RejectionAtomicity(unittest.TestCase):
    def setUp(self):
        fixtures.DeleteOrders.setUp(self)
        self.heads=self.r.headers()  # Keep complete-parent prepare/apply injection coverage.
    def corrupt_manifest(self,db):
        blob=bytearray(c.put(db,c.prepare({'probe':'完整性测试'})))
        blob[len(c.MAGIC)]^=1
        return bytes(blob)
    def test_corrupt_orphan_work_manifest_rejected_before_reclamation(self):
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('INSERT INTO normal_work_revisions VALUES(?,?,?)',('unreferenced','2000-01-01',self.corrupt_manifest(db)))
        plan=m.prepare(self.path,self.heads);before=self.dump()
        with self.assertRaisesRegex(ValueError,'校验'):m.execute(self.path,plan)
        self.assertEqual(self.dump(),before)
    def test_corrupt_old_signed_revision_rejected_before_parent_deletion(self):
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',('oid',0,'historical',wr.now(),self.corrupt_manifest(db)))
        plan=m.prepare(self.path,self.heads);before=self.dump()
        with self.assertRaisesRegex(ValueError,'校验'):m.execute(self.path,plan)
        self.assertEqual(self.dump(),before)
    def dump(self):
        with closing(sqlite3.connect(self.path)) as db:return list(db.iterdump())
    def inject(self):
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('CREATE TABLE IF NOT EXISTS scheduled_values(payload BLOB)')
            db.execute('INSERT INTO scheduled_values VALUES(?)',(zlib.compress(b'{"raw": "retained"}'),))
    def test_unselected_old_owner_blocks_preview_without_changes(self):
        self.inject();before=self.dump()
        with self.assertRaisesRegex(ValueError,'已停用'):m.prepare(self.path,self.heads)
        self.assertEqual(self.dump(),before)
    def test_old_owner_added_after_preparation_rolls_back_and_retry_works(self):
        plan=m.prepare(self.path,self.heads);original=m.prepare_rewrites;before=[]
        def inject(*args):
            result=original(*args);self.inject();before.extend(self.dump());return result
        with patch.object(m,'prepare_rewrites',side_effect=inject),self.assertRaisesRegex(ValueError,'已停用'):m.execute(self.path,plan)
        self.assertEqual(self.dump(),before);self.assertEqual(len(self.r.headers()),1)
        with closing(sqlite3.connect(self.path)) as db,db:db.execute('DROP TABLE scheduled_values')
        result=m.execute(self.path,m.prepare(self.path,self.heads));self.assertEqual(result['deleted'],1)
    def test_old_draft_cannot_be_deleted_before_format_check(self):
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(wr.today(),'old','digest','stamp','{}',zlib.compress(b'{}')))
        before=self.dump()
        with self.assertRaisesRegex(ValueError,'已停用'):m.delete_job(self.path,wr.today(),'old')
        self.assertEqual(self.dump(),before)

class Backups(unittest.TestCase):
    setUp=fixtures.DeleteOrders.setUp
    def test_current_backup_preserves_every_table_and_source(self):
        target=self.root/'backup.sqlite3';before=self.path.read_bytes();result=backup.backup(self.path,target)
        self.assertEqual(self.path.read_bytes(),before);self.assertFalse(result['source_modified'])
        with closing(sqlite3.connect(self.path)) as a,closing(sqlite3.connect(target)) as b:self.assertEqual(list(a.iterdump()),list(b.iterdump()))
        self.assertEqual(result['records']['normal_revisions'],1)
    def test_existing_destination_not_overwritten(self):
        target=self.root/'backup.sqlite3';target.write_bytes(b'keep')
        with self.assertRaisesRegex(ValueError,'覆盖'):backup.backup(self.path,target)
        self.assertEqual(target.read_bytes(),b'keep')
    def test_cancel_cleans_temporary_without_publishing(self):
        target=self.root/'cancelled.sqlite3';event=threading.Event();before=self.path.read_bytes()
        with self.assertRaises(Cancelled):backup.backup(self.path,target,lambda _:event.set(),event)
        self.assertFalse(target.exists());self.assertEqual(list(self.root.glob('.*.tmp')),[]);self.assertEqual(self.path.read_bytes(),before)
    def test_bad_block_rejects_backup_and_keeps_source(self):
        with closing(sqlite3.connect(self.path)) as db,db:db.execute('UPDATE snapshot_chunks SET size=size+1')
        target=self.root/'bad.sqlite3';before=self.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'校验'):backup.backup(self.path,target)
        self.assertFalse(target.exists());self.assertEqual(self.path.read_bytes(),before)
    def test_old_format_rejects_backup_and_keeps_source(self):
        with closing(sqlite3.connect(self.path)) as db,db:db.execute('UPDATE normal_work_revisions SET payload=?',(zlib.compress(b'{}'),))
        target=self.root/'old.sqlite3';before=self.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'已停用'):backup.backup(self.path,target)
        self.assertFalse(target.exists());self.assertEqual(self.path.read_bytes(),before)

if __name__=='__main__':unittest.main()
