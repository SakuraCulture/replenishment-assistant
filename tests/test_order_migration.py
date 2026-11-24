"""Complete migration: synthetic only, backup retained, interrupted rename rollback."""
import copy,json,os,shutil,sqlite3,tempfile,threading,unittest,zipfile
from pathlib import Path
from contextlib import closing,contextmanager
from unittest.mock import patch
import test_archive_orders as fixtures
import order_migration as migration
import work_repository as wr
import parameter_schema
from stockout_common import file_hash,atomic_json

def files(root):return {p.relative_to(root).as_posix():file_hash(p) for p in root.rglob('*') if p.is_file()}


@contextmanager
def exclusive_read(path):
    """Reproduce the Windows sharing violation without a real browser."""
    import ctypes
    from ctypes import wintypes as w
    from migration_paths import io_path
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,ctypes.c_void_p,w.DWORD,w.DWORD,w.HANDLE]
    kernel.CreateFileW.restype=w.HANDLE
    kernel.CloseHandle.argtypes=[w.HANDLE]
    handle=kernel.CreateFileW(str(io_path(path)),0x80000000,0,None,3,0,None)
    if handle==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
    try:yield
    finally:kernel.CloseHandle(handle)


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
    def test_readonly_preflight_does_not_create_a_verification_copy(self):
        self.export();before=files(self.target)
        with patch('order_migration.db_copy',side_effect=AssertionError('Unexpected staging copy')):
            plan=migration.prepare(self.target,self.package)
        self.assertEqual(files(self.target),before)
        self.assertFalse(any(Path(plan['stage']).rglob('*.verified')))
        # A sidecar appearing after preview is outside the frozen ZIP members.
        (Path(plan['stage'])/'workbench.sqlite3-journal').touch()
        with self.assertRaisesRegex(ValueError,'未列入包的日志文件'):migration.apply(plan)
        self.assertEqual(files(self.target),before)

    @unittest.skipUnless(os.name=='nt','Windows status file sharing')
    def test_transient_windows_status_sharing_error_retries_only_the_status(self):
        import ctypes
        import data_location_status
        from migration_status import record,latest
        plan=self.prepare();before=files(self.target);blocked=[]
        original=data_location_status.atomic_json
        def publish(path,value):
            if Path(path).name.endswith('.status.json') and not blocked:
                blocked.append(path)
                raise ctypes.WinError(5)
            return original(path,value)
        with patch('data_location_status.atomic_json',side_effect=publish):
            record(plan,'APPLYING',phase='COPYING')
        self.assertEqual(len(blocked),1)
        self.assertEqual(latest(self.target)['phase'],'COPYING')
        self.assertEqual(files(self.target),before)

    def test_snapshot_validation_reads_projected_children_once_and_hashes_other_fields(self):
        from maintenance_storage import Editor
        import storage_chunks as chunks
        with closing(sqlite3.connect(self.work.path)) as db,closing(sqlite3.connect(':memory:')) as scratch:
            payload=copy.deepcopy(self.payload)
            payload['extra']='验证整个快照'
            manifest=chunks.require_manifest(fixtures.no.encode(payload,db))
            editor=Editor(db,scratch)
            with patch.object(editor,'raw',wraps=editor.raw) as checksum_reads:
                editor.checksum(manifest['tree'])
            with patch.object(editor,'raw',wraps=editor.raw) as raw:
                actual=editor.checked_fields(manifest,('result','new_focus'))
            self.assertEqual(actual,{key:editor.get(manifest['tree'],(key,)) for key in ('result','new_focus')})
            self.assertEqual(raw.call_args_list,checksum_reads.call_args_list)
            extra=next(v for k,v in manifest['tree'][1] if k=='extra')
            db.execute('DELETE FROM snapshot_chunks WHERE hash=?',(extra[1],))
            with self.assertRaisesRegex(ValueError,'内容块缺失'):
                Editor(db,scratch).checked_fields(manifest,('result','new_focus'))
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

    def test_deep_catalog_requests_survive_staging_and_both_import_modes(self):
        from order_merge_migration import apply as merge_apply

        if os.name=='nt':
            cleanup_root=Path('\\\\?\\'+str(self.root.resolve()))
            self.addCleanup(shutil.rmtree,cleanup_root)
        member=Path('jst_catalog_sync/downloads')/('a'*36)/('b'*36)/('validate-'+'c'*36+'.request.json')
        original=self.source/member
        original.parent.mkdir(parents=True)
        original.write_bytes(b'{"fixture":"source"}')
        package_digest=self.export()['sha256']
        for mode,apply in (('merge',merge_apply),('replace',migration.apply)):
            with self.subTest(mode=mode):
                target=self.root/('test-profile-'+mode)/'settings'
                wr.Repository(target)
                local=target/member
                local.parent.mkdir(parents=True)
                local.write_bytes(b'{"fixture":"local"}')
                plan=migration.prepare(target,self.package,mode=mode)
                stage=Path(plan['stage'])
                if os.name=='nt':
                    self.assertGreaterEqual(len(str(stage/member)),260)
                    self.assertGreaterEqual(len(str(Path(plan['new'])/member)),260)
                    prepared=Path('\\\\?\\'+str(stage/member))
                    backup=Path('\\\\?\\'+str(Path(plan['backup'])/member))
                else:
                    prepared=stage/member
                    backup=Path(plan['backup'])/member
                self.assertEqual(prepared.read_bytes(),original.read_bytes())
                self.assertEqual(local.read_bytes(),b'{"fixture":"local"}')
                with patch('application_reset._check_other_processes'),patch('order_merge_migration._check_other_processes'):
                    result=apply(plan)
                self.assertEqual(result['state'],'COMPLETED')
                self.assertEqual(local.read_bytes(),original.read_bytes() if mode=='replace' else b'{"fixture":"local"}')
                self.assertEqual(backup.read_bytes(),b'{"fixture":"local"}')
                self.assertEqual(migration.info(target/'workbench.sqlite3')['normal_orders'],1)
                self.assertFalse(stage.exists())
                self.assertEqual(file_hash(self.package),package_digest)

    @unittest.skipUnless(os.name=='nt','requires Windows file sharing semantics')
    def test_busy_profiles_stop_before_staging_and_again_before_copy(self):
        from order_migration_browsers import BrowserProfileBusy
        self.export()
        for mode, suffix, name, module in (
            ('merge','hb','河北仓','order_merge_migration'),
            ('replace','sd','山东仓','application_reset'),
        ):
            with self.subTest(mode=mode):
                target=self.root/mode/'settings'
                wr.Repository(target)
                browser=target/f'jst_catalog_sync/browsers/{suffix}/User Data/Default/Network/Cookies'
                browser.parent.mkdir(parents=True)
                browser.write_bytes(b'synthetic browser bytes')
                original=files(target)
                with patch('data_relocation.browser_rows',return_value=[]),patch(module+'._check_other_processes'):
                    with exclusive_read(browser),self.assertRaises(BrowserProfileBusy) as raised:
                        migration.prepare(target,self.package,mode=mode)
                    self.assertEqual(raised.exception.warehouses,(name,))
                    self.assertFalse(list(target.parent.glob('.settings.order-import-*')))
                    plan=migration.prepare(target,self.package,mode=mode)
                    with exclusive_read(browser),self.assertRaises(BrowserProfileBusy):
                        migration.apply(plan)
                    self.assertFalse(Path(plan['new']).exists())
                    self.assertFalse(Path(plan['backup']).exists())
                    self.assertEqual(files(target),original)
                    result=migration.apply(plan)
                self.assertEqual(result['state'],'COMPLETED')
                self.assertEqual(browser.read_bytes(),b'synthetic browser bytes')
                self.assertEqual(files(Path(result['backup'])),original)

    @unittest.skipUnless(os.name=='nt','requires Windows file sharing semantics')
    def test_browser_opened_after_preflight_identifies_late_copy_failure(self):
        from migration_status import latest
        from order_migration_browsers import BrowserProfileBusy
        self.export()
        browser=self.target/'jst_catalog_sync/browsers/hb/User Data/Default/Sessions/Tabs_synthetic'
        browser.parent.mkdir(parents=True)
        browser.write_bytes(b'synthetic')
        with patch('data_relocation.browser_rows',return_value=[]):
            plan=migration.prepare(self.target,self.package,mode='merge')
        original=files(self.target)
        with exclusive_read(browser),patch('order_merge_migration.require_closed'),patch('order_merge_migration._check_other_processes'):
            with self.assertRaises(BrowserProfileBusy) as raised:
                migration.apply(plan)
        self.assertEqual(raised.exception.warehouses,('河北仓',))
        self.assertIn('[WinError 32]',str(raised.exception.__cause__))
        self.assertEqual(latest(self.target)['phase'],'COPYING')
        self.assertFalse(Path(plan['backup']).exists())
        self.assertEqual(files(self.target),original)

    @unittest.skipUnless(os.name=='nt','requires Windows process argument handling')
    def test_only_local_profiles_with_warehouse_bindings_are_checked(self):
        from order_migration_browsers import BrowserProfileBusy,require_closed
        catalog=self.target/'jst_catalog_sync';catalog.mkdir()
        local=self.target/'custom sd edge'/'Profile 1'
        external=self.root/'daily edge'/'Default'
        for profile in (local,external):profile.mkdir(parents=True)
        atomic_json(catalog/'jobs.json',{'accounts':{
            name:{'binding':{'userDataRoot':str(profile.parent),'actualProfilePath':str(profile)}}
            for name,profile in (('山东仓',local),('河北仓',external))}})
        rows=[{'CommandLine':None},{'CommandLine':f'edge.exe --user-data-dir="{external.parent}"'}]
        with patch('data_relocation.browser_rows',return_value=rows):
            require_closed(self.target)
            rows.append({'CommandLine':f'edge.exe --user-data-dir "{local.parent}"'})
            with self.assertRaises(BrowserProfileBusy) as raised:
                require_closed(self.target)
        self.assertEqual(raised.exception.warehouses,('山东仓',))

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
    def test_insufficient_disk_reports_failure_and_keeps_source(self):
        before=files(self.source)
        disk=shutil.disk_usage(self.root)._replace(free=1)
        with patch('shutil.disk_usage',return_value=disk):
            with self.assertRaisesRegex(ValueError,'磁盘余量不足') as failed:
                self.export()
        self.assertIn(str(self.package.parent),str(failed.exception))
        self.assertFalse(self.package.exists())
        self.assertEqual(files(self.source),before)
    def test_zip_is_published_only_after_verification_and_cancellation_check(self):
        from runtime_support import Cancelled
        stages=[]
        def progress(message):
            stages.append(str(message))
            if str(message).startswith('正在核验迁移包摘要'):
                self.assertFalse(self.package.exists())
                self.cancel.set()
        with self.assertRaises(Cancelled):
            migration.export(self.source,self.package,progress,cancel=self.cancel)
        self.assertIn('正在从订单及快照查找原件引用…',stages)
        self.assertTrue(any(stage.startswith('正在压缩') for stage in stages))
        self.assertFalse(self.package.exists())
        self.assertEqual(list(self.root.glob('.migration-*.zip')),[])
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
