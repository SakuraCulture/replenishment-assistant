"""Real ZIP preparation, private-copy publication and recoverable failures."""
import json,sqlite3,tempfile,threading,unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import merge_fixture as f
import migration_status
import normal_orders
import order_migration as migration
import order_merge_migration as merging
import work_repository
from application_reset import InstanceLease


class MergeHandoff(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.source=f.Order(self.root/'source','SOURCE');self.target=f.Order(self.root/'target','LOCAL')
        self.kept=f.keep_local_files(self.target);self.counter=0
    def prepare(self):
        self.counter+=1;package=self.root/f'orders-{self.counter}.zip';self.source.export(package)
        return merging.prepare(self.target.root,package)
    def apply(self,plan):
        with patch('order_merge_migration._check_other_processes'):return migration.apply(plan)
    def test_real_merge_keeps_complete_original_backup_and_local_business_data(self):
        self.source.complete();local=self.target.signed();before=f.file_hashes(self.target.root);plan=self.prepare()
        self.assertEqual(plan['mode'],'merge');self.assertEqual(plan['merge_report']['counts']['added'],1)
        self.assertEqual(f.file_hashes(self.target.root),before)
        result=self.apply(plan)
        self.assertEqual(result['state'],'COMPLETED');self.assertEqual(result['counts']['added'],1)
        self.assertEqual(f.file_hashes(Path(result['backup'])),before)
        repo=normal_orders.Repository(work_repository.Repository(self.target.root))
        self.assertEqual(repo.get(f.DAY,'LOCAL'),local);self.assertEqual(repo.get(f.DAY,'SOURCE'),self.source.signed())
        for name,raw in self.kept.items():self.assertEqual((self.target.root/name).read_bytes(),raw,name)
        receipt=json.loads((self.target.root/'订单迁入完成.json').read_text('utf-8'))
        self.assertEqual(receipt['mode'],'merge');self.assertTrue(Path(result['report']).is_file())
        self.assertIsNone(migration.recover(self.target.root));self.assertFalse(Path(plan['stage']).exists())
    def test_repeated_zip_preview_is_duplicate_and_does_not_change_target(self):
        self.apply(self.prepare());before=f.file_hashes(self.target.root);plan=self.prepare()
        self.assertEqual(plan['merge_report']['counts']['added'],0);self.assertEqual(plan['merge_report']['counts']['duplicate'],1)
        migration.discard(plan);self.assertEqual(f.file_hashes(self.target.root),before)
        self.assertEqual(migration_status.latest(self.target.root)['state'],'CANCELLED')
    def test_target_changed_after_preview_rejects_without_silent_scope_reduction(self):
        plan=self.prepare();f.second_order(self.target,'SOURCE',oid='different-oid')
        before=f.file_hashes(self.target.root)
        with self.assertRaisesRegex(ValueError,'变化|重新|预览|冲突'):self.apply(plan)
        self.assertEqual(f.file_hashes(self.target.root),before);self.assertFalse(Path(plan['backup']).exists())
        self.assertEqual(migration_status.latest(self.target.root)['state'],'FAILED')
    def test_copy_failure_retains_first_error_source_package_and_original(self):
        plan=self.prepare();before=f.file_hashes(self.target.root)
        with patch('order_merge_migration._clone',side_effect=OSError('ORIGINAL-MERGE-COPY-FAIL')):
            with self.assertRaisesRegex(OSError,'ORIGINAL-MERGE-COPY-FAIL'):self.apply(plan)
        self.assertEqual(f.file_hashes(self.target.root),before);self.assertTrue(Path(plan['stage']).is_dir())
        self.assertTrue(Path(plan['package']).is_file())
        self.assertIn('ORIGINAL-MERGE-COPY-FAIL',migration_status.latest(self.target.root)['error']['traceback'])
    def test_second_rename_failure_restores_original_and_preserves_materials(self):
        plan=self.prepare();before=f.file_hashes(self.target.root);rename=Path.rename
        def failure(path,destination):
            if path==Path(plan['new']):raise OSError('ORIGINAL-MERGE-RENAME-FAIL')
            return rename(path,destination)
        with patch.object(Path,'rename',failure):
            with self.assertRaisesRegex(OSError,'ORIGINAL-MERGE-RENAME-FAIL'):self.apply(plan)
        self.assertEqual(f.file_hashes(self.target.root),before);self.assertTrue(Path(plan['stage']).is_dir())
        self.assertFalse(migration.journal(self.target.root).exists())
    def test_prepared_source_tampering_is_rejected_before_clone(self):
        plan=self.prepare();before=f.file_hashes(self.target.root)
        (Path(plan['stage'])/'workbench.sqlite3').write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError,'变化'):self.apply(plan)
        self.assertEqual(f.file_hashes(self.target.root),before);self.assertFalse(Path(plan['new']).exists())
    def test_live_instance_lease_cannot_be_bypassed(self):
        plan=self.prepare();before=f.file_hashes(self.target.root);lease=InstanceLease(self.target.root)
        try:
            with self.assertRaisesRegex(ValueError,'窗口|使用'):self.apply(plan)
        finally:lease.close()
        self.assertEqual(f.file_hashes(self.target.root),before)
    def test_foreign_database_writer_cannot_be_bypassed(self):
        plan=self.prepare();before=f.file_hashes(self.target.root)
        with closing(sqlite3.connect(self.target.path)) as db:
            db.execute('BEGIN IMMEDIATE')
            with self.assertRaisesRegex(ValueError,'写入|安全|原任务'):self.apply(plan)
        self.assertEqual(f.file_hashes(self.target.root),before)
    def test_prepare_cancel_keeps_original_and_zip(self):
        package=self.root/'cancel.zip';self.source.export(package);before=f.file_hashes(self.target.root)
        cancel=threading.Event();cancel.set()
        with self.assertRaises(Exception):merging.prepare(self.target.root,package,cancel=cancel)
        self.assertEqual(f.file_hashes(self.target.root),before);self.assertTrue(package.is_file())
    def test_full_replacement_remains_separate_and_rebinds_new_export_receipts(self):
        import export_receipts,normal_exports
        exported=self.source.folder/'signed.xlsx';normal_exports.export(self.source.signed(),exported,warehouse='河北仓')
        receipt=export_receipts.begin(self.source.path,'merchant',[{'path':str(exported),'head':export_receipts.identity(self.source.signed(),'河北仓'),'state':'已生成'}])
        package=self.root/'replace.zip';self.source.export(package)
        self.assertTrue(any(e['path'].startswith('export_receipts/') for e in migration.manifest(package)['entries']))
        plan=migration.prepare(self.target.root,package)
        self.assertNotEqual(plan.get('mode'),'merge')
        with patch('application_reset._check_other_processes'):result=migration.apply(plan)
        self.assertEqual(result['state'],'COMPLETED')
        recent=export_receipts.recent(self.target.path)
        self.assertEqual(len(recent),1);self.assertEqual(recent[0]['id'],receipt['id'])
        self.assertEqual(recent[0]['files'],receipt['files']);self.assertEqual(recent[0]['database'],str(self.target.path.resolve()))
        self.assertEqual(len(f.table_rows(self.target.path,'normal_orders')),1,'Legacy entry intentionally replaces, not merges')


if __name__=='__main__':unittest.main()
