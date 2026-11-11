"""Complete migration: synthetic only, backup retained, interrupted rename rollback."""
import copy,json,sqlite3,tempfile,threading,unittest,zipfile
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
import test_archive_orders as fixtures
import order_migration as migration
import work_repository as wr
import parameter_schema
from stockout_common import file_hash,atomic_json

def files(root):return {p.relative_to(root).as_posix():file_hash(p) for p in root.rglob('*') if p.is_file()}

class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);source=self.root/'source';source.mkdir();self.work,self.orders,self.payload=fixtures.seed(source);self.source=self.work.path.parent
        self.target=self.root/'target';self.target.mkdir();wr.Repository(self.target).save_preset('原机预案',parameter_schema.standard())
        (self.target/'connection.json').write_text('target-private');(self.target/'analysis').mkdir();(self.target/'analysis'/'independent').write_bytes(b'analysis-unchanged')
        (self.source/'connection.json').write_text('source-secret-DO-NOT-EXPORT');(self.source/'aoxiang'/'component-state'/'config').mkdir(parents=True)
        (self.source/'aoxiang'/'component-state'/'config'/'machine.binding.json').write_text('cookie-secret-DO-NOT-EXPORT')
        wf=self.source/'aoxiang'/'workflows.sqlite3'
        with closing(sqlite3.connect(wf)) as db,db:
            db.execute('CREATE TABLE ax_workflows(id TEXT PRIMARY KEY,state TEXT,payload TEXT)');db.execute('INSERT INTO ax_workflows VALUES(?,?,?)',('run','RUNNING',json.dumps({'state':'RUNNING','items':[{'platform_order':'PO-unique','state':'BOUND'}]})))
            db.execute('CREATE TABLE ax_schedules(id TEXT PRIMARY KEY,payload TEXT)');db.execute('INSERT INTO ax_schedules VALUES(?,?)',('schedule',json.dumps({'enabled':True,'platform_order':'PO-unique'})))
        self.cancel=threading.Event();self.package=self.root/'orders.zip'
    def export(self):return migration.export(self.source,self.package,cancel=self.cancel)
    def prepare(self):self.export();return migration.prepare(self.target,self.package,cancel=self.cancel)
    def test_full_archive_readonly_source_and_secret_exclusion(self):
        before=files(self.source);result=self.export();self.assertEqual(files(self.source),before);manifest=migration.manifest(self.package)
        with closing(sqlite3.connect(self.work.path)) as db:original_count=len(db.execute('SELECT sha FROM source_files').fetchall())
        # XLSX ZIP timestamps can make the two equal logical masters either
        # byte-identical or distinct. Both exact source identities must survive.
        self.assertEqual(manifest['counts']['normal_orders'],1);self.assertEqual(manifest['counts']['source_files'],original_count);self.assertEqual(sum(e['path'].startswith('migration-assets/') for e in manifest['entries']),2)
        with zipfile.ZipFile(self.package) as book:
            self.assertNotIn('connection.json',book.namelist());self.assertFalse(any('machine.binding' in n for n in book.namelist()));self.assertIn('aoxiang/workflows.sqlite3',book.namelist())
        self.assertEqual(result['sha256'],file_hash(self.package))
    def test_full_restore_keeps_frozen_orders_target_connections_and_backup(self):
        source_payload=copy.deepcopy(self.orders.get(wr.today(),'T'));before=files(self.target);plan=self.prepare();self.assertEqual(files(self.target),before)
        with patch('application_reset._check_other_processes'):
            result=migration.apply(plan)
        self.assertEqual(result['state'],'COMPLETED');self.assertEqual(files(Path(result['backup'])),before)
        restored=fixtures.no.Repository(wr.Repository(self.target));self.assertEqual(restored.get(wr.today(),'T'),source_payload)
        self.assertEqual((self.target/'connection.json').read_text(),'target-private');self.assertEqual((self.target/'analysis'/'independent').read_bytes(),b'analysis-unchanged')
        with closing(sqlite3.connect(self.target/'aoxiang'/'workflows.sqlite3')) as db:
            state,raw=db.execute('SELECT state,payload FROM ax_workflows').fetchone();self.assertEqual(state,'PAUSED');self.assertEqual(json.loads(raw)['items'][0]['platform_order'],'PO-unique')
            self.assertFalse(json.loads(db.execute('SELECT payload FROM ax_schedules').fetchone()[0])['enabled'])
        mapping=json.loads((self.target/'迁入原文件映射.json').read_text('utf-8'))
        for relative in mapping['paths'].values():self.assertTrue((self.target/relative).is_file())
        self.assertFalse(Path(plan['stage']).exists());self.assertIsNone(migration.recover(self.target))
    def test_failed_rename_rolls_back_whole_target(self):
        plan=self.prepare();before=files(self.target);rename=Path.rename
        def fail_new(path,dest):
            if path==Path(plan['new']):raise OSError('模拟第二次重命名失败')
            return rename(path,dest)
        with patch('application_reset._check_other_processes'),patch.object(Path,'rename',fail_new):
            with self.assertRaisesRegex(OSError,'模拟'):migration.apply(plan)
        self.assertEqual(files(self.target),before);self.assertFalse(migration.journal(self.target).exists())
    def test_startup_recovery_after_first_move(self):
        plan=self.prepare();before=files(self.target);atomic_json(migration.journal(self.target),plan);self.target.rename(plan['backup'])
        result=migration.recover(self.target);self.assertEqual(result['state'],'ROLLED_BACK');self.assertEqual(files(self.target),before)
    def test_modified_package_and_zip_slip_rejected_before_target_changes(self):
        self.export();before=files(self.target)
        with zipfile.ZipFile(self.package,'a') as book:book.writestr('../escape.txt','bad')
        with self.assertRaisesRegex(ValueError,'集合'):migration.prepare(self.target,self.package)
        self.assertEqual(files(self.target),before);self.assertFalse((self.root/'escape.txt').exists())
    def test_prepared_tampering_and_forged_plan_cannot_write_target(self):
        plan=self.prepare();before=files(self.target);(Path(plan['stage'])/'workbench.sqlite3').write_bytes(b'bad')
        with self.assertRaisesRegex(ValueError,'变化'):migration.apply(plan)
        forged={**plan,'new':str(self.root/'unrelated')}
        with self.assertRaises(ValueError):migration.apply(forged)
        self.assertEqual(files(self.target),before)
    def test_target_writer_prevents_restore_without_replacing_anything(self):
        plan=self.prepare();before=files(self.target)
        with closing(sqlite3.connect(self.target/'workbench.sqlite3')) as db,patch('application_reset._check_other_processes'):
            db.execute('BEGIN IMMEDIATE')
            with self.assertRaisesRegex(ValueError,'资料未替换'):migration.apply(plan)
        self.assertEqual(files(self.target),before);self.assertFalse(Path(plan['backup']).exists())
    def test_cancel_or_bad_original_leaves_no_published_archive(self):
        self.cancel.set()
        with self.assertRaises(Exception):self.export()
        self.assertFalse(self.package.exists());self.cancel.clear()
        with closing(sqlite3.connect(self.work.path)) as db,db:db.execute("UPDATE source_files SET content=X'00'")
        with self.assertRaisesRegex(ValueError,'原件摘要'):self.export()
        self.assertFalse(self.package.exists())
    def test_foreign_writer_blocks_export_no_partial_package(self):
        with closing(sqlite3.connect(self.work.path)) as db:
            db.execute('BEGIN IMMEDIATE')
            with self.assertRaises(sqlite3.OperationalError):self.export()
        self.assertFalse(self.package.exists())
    def test_wal_database_is_exported_by_backup_not_raw_copy(self):
        with closing(sqlite3.connect(self.work.path)) as db:
            db.execute('PRAGMA journal_mode=WAL');db.execute('CREATE TABLE migration_evidence(value TEXT)');db.execute("INSERT INTO migration_evidence VALUES('committed-in-wal')");db.commit()
            self.assertTrue(Path(str(self.work.path)+'-wal').exists());self.export()
        plan=migration.prepare(self.target,self.package)
        with closing(sqlite3.connect(Path(plan['stage'])/'workbench.sqlite3')) as db:self.assertEqual(db.execute('SELECT value FROM migration_evidence').fetchone()[0],'committed-in-wal')

    def test_second_migration_preserves_old_path_aliases(self):
        plan=self.prepare()
        with patch('application_reset._check_other_processes'):migration.apply(plan)
        old=json.loads((self.target/'迁入原文件映射.json').read_text('utf-8'))['paths']
        package=self.root/'second.zip';migration.export(self.target,package)
        current=migration.manifest(package)['external_map']
        self.assertTrue(set(old.items())<=set(current.items()))


if __name__=='__main__':unittest.main()
