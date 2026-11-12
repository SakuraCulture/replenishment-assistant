"""Whole-storage relocation in generated directories; never opens real app data."""
import json,os,sqlite3,subprocess,sys,tempfile,unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import data_location as location,data_relocation as move,directory_links
import test_archive_orders as fixture
import work_repository as wr

class DataLocation(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        env=patch.dict(os.environ,LOCALAPPDATA=str(self.root/'localappdata'));env.start();self.addCleanup(env.stop)
        self.previous=os.environ.pop('REPLENISHMENT_DATA_DIR',None)
        self.addCleanup(lambda:os.environ.__setitem__('REPLENISHMENT_DATA_DIR',self.previous) if self.previous is not None else os.environ.pop('REPLENISHMENT_DATA_DIR',None))
        self.source=location.default_base();self.source.mkdir(parents=True);self.target=self.root/'新资料位置'
        self.work,self.repo,self.payload=fixture.seed(self.source)
        self.browser=patch.object(move,'browser_rows',return_value=[]);self.browser.start();self.addCleanup(self.browser.stop)
        self.peers=patch('application_reset._check_other_processes');self.peers.start();self.addCleanup(self.peers.stop)
        profile=self.source/'settings/jst_catalog_sync/browsers/sd/User Data/Default';profile.mkdir(parents=True)
        (profile.parent/'Local State').write_text('synthetic-profile-state','utf-8')
        (self.source/'settings/analysis-store').mkdir()
        with closing(sqlite3.connect(self.source/'settings/analysis-store/analysis.sqlite3')) as db,db:
            db.execute('CREATE TABLE facts(sku TEXT,stock INTEGER)');db.execute("INSERT INTO facts VALUES('001',0)")
        (self.source/'settings/analysis-store/original.xlsx').write_bytes(b'synthetic-original')
        (self.source/'.settings.order-import-old').mkdir();(self.source/'.settings.order-import-old/prepared.bin').write_bytes(b'pending-preserved')
        (self.source/'AI接入').mkdir();(self.source/'AI接入/binding.json').write_text('synthetic-registration','utf-8')
        self.before=self.business()
    def business(self,base=None):
        root=base or self.source
        with closing(sqlite3.connect(root/'settings/workbench.sqlite3')) as db:
            return {name:db.execute('SELECT * FROM '+name+' ORDER BY 1,2').fetchall() for name in ('normal_orders','normal_revisions','normal_work_revisions','source_files')}
    def prepare(self):return move.prepare(self.source,self.target)
    def apply(self):
        plan=self.prepare();return plan,move.apply(plan)
    def test_automatic_paths_use_private_localappdata_and_explicit_defaults_remain_supported(self):
        self.assertEqual(location.root(),self.source/'settings');self.assertEqual(location.hint(),self.source/'settings')
        explicit=self.root/'explicit-default'
        self.assertEqual(location.root(explicit),explicit/'settings');self.assertEqual(location.hint(explicit),explicit/'settings')
    def test_preparation_cannot_read_or_move_another_dataset(self):
        other=self.root/'another-dataset';other.mkdir()
        with patch.object(move,'files',side_effect=AssertionError('Must not enumerate another dataset')):
            with self.assertRaisesRegex(ValueError,'不是当前'):move.prepare(other,self.target)
        self.assertFalse(self.target.exists());self.assertIsNone(move.status())
    def test_apply_never_uses_a_plan_after_active_data_resolution_changes(self):
        plan=self.prepare()
        with patch.object(location,'base',return_value=self.root/'another-dataset'),self.assertRaisesRegex(ValueError,'与搬迁计划不同'):move.apply(plan)
        self.assertEqual(self.business(),self.before);self.assertFalse(self.target.exists())
    def test_complete_move_keeps_orders_analysis_originals_pending_and_login_files(self):
        old=self.source/'河北仓.xlsx';identity=location.identity_path(self.work.path)
        plan,result=self.apply()
        self.assertEqual((result['state'],result['phase']),('COMPLETED','COMPLETED'))
        self.assertEqual(location.root(),self.target/'settings');self.assertEqual(self.business(self.target),self.before)
        self.assertTrue(self.source.is_junction());self.assertEqual(self.source.resolve(),self.target.resolve());self.assertTrue(old.is_file())
        self.assertEqual((self.target/'settings/analysis-store/original.xlsx').read_bytes(),b'synthetic-original')
        self.assertEqual((self.target/'.settings.order-import-old/prepared.bin').read_bytes(),b'pending-preserved')
        self.assertEqual((self.target/'AI接入/binding.json').read_text(),'synthetic-registration')
        self.assertTrue((self.target/'settings/jst_catalog_sync/browsers/sd/User Data/Default').is_dir())
        self.assertEqual(location.identity_path(self.target/'settings/workbench.sqlite3'),identity)
        self.assertEqual(location.aliases_for(self.target/'settings'),[{'from':str(self.source),'to':str(self.target)}])
        archive=Path(plan['archive']);self.assertTrue(archive.is_file());move.verify_archive(archive,plan)
        remaining=sum(p.stat().st_size for p in Path(plan['backup']).rglob('*') if p.is_file()) if Path(plan['backup']).exists() else 0
        self.assertLess(remaining,1024*1024)
    def test_explicit_environment_still_wins_and_ignores_machine_config(self):
        self.apply()
        with patch.dict(os.environ,REPLENISHMENT_DATA_DIR=str(self.root/'isolated')):
            self.assertEqual(location.root(),self.root/'isolated');self.assertEqual(location.aliases_for(self.target/'settings'),[])
    def test_cancellation_does_not_copy_or_switch(self):
        plan=self.prepare();move.cancel(plan)
        self.assertEqual(move.status()['state'],'CANCELLED');self.assertFalse(self.target.exists());self.assertIsNone(location.config())
        self.assertEqual(self.business(),self.before)
    def test_nonempty_target_and_overlap_never_overwrite(self):
        self.target.mkdir();(self.target/'keep').write_text('keep')
        with self.assertRaisesRegex(ValueError,'为空'):self.prepare()
        self.assertEqual((self.target/'keep').read_text(),'keep')
        for path in (self.source,self.source/'child',self.source.parent):
            with self.subTest(path=path),self.assertRaises(ValueError):move.prepare(self.source,path)
    def test_insufficient_space_preserves_original(self):
        class Disk:free=0
        with patch.object(move.shutil,'disk_usage',return_value=Disk()),self.assertRaisesRegex(ValueError,'空间不足'):self.prepare()
        self.assertIsNone(location.config());self.assertEqual(self.business(),self.before)
    def test_live_instance_and_foreign_database_writer_block_copy(self):
        plan=self.prepare()
        with patch('application_reset._check_other_processes',side_effect=ValueError('LIVE-INSTANCE')):
            with self.assertRaisesRegex(ValueError,'LIVE-INSTANCE'):move.apply(plan)
        self.assertEqual(move.status()['state'],'FAILED');self.assertEqual(self.business(),self.before)
        plan=self.prepare()
        with closing(sqlite3.connect(self.work.path)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            with self.assertRaisesRegex(ValueError,'仍被'):move.apply(plan)
        self.assertEqual(self.business(),self.before);self.assertFalse(self.target.exists())
    def test_only_profiles_inside_moving_data_require_browser_close(self):
        row={'ProcessId':123,'CommandLine':'msedge.exe --user-data-dir="'+str(self.source/'settings/jst_catalog_sync/browsers/sd/User Data')+'"'}
        with patch.object(move,'browser_rows',return_value=[row]),self.assertRaisesRegex(ValueError,'专用浏览器'):self.prepare()
        row['CommandLine']='msedge.exe --user-data-dir="'+str(self.root/'unrelated-profile')+'"'
        with patch.object(move,'browser_rows',return_value=[row]):plan=self.prepare()
        move.cancel(plan)
        with patch.object(move,'browser_rows',return_value=[{'ProcessId':999,'CommandLine':None}]),self.assertRaisesRegex(ValueError,'无法核实'):self.prepare()
    def test_links_inside_source_are_rejected_without_traversal(self):
        external=self.root/'external';external.mkdir();(external/'keep').write_text('keep')
        directory_links.create(self.source/'external-link',external)
        with self.assertRaisesRegex(ValueError,'目录链接'):self.prepare()
        self.assertEqual((external/'keep').read_text(),'keep')
    def test_changed_source_after_copy_is_detected_before_switch(self):
        plan=self.prepare();original=move._copy
        def changed(*args):
            original(*args);(self.source/'settings/analysis-store/original.xlsx').write_bytes(b'new-external-change')
        with patch.object(move,'_copy',changed),self.assertRaisesRegex(ValueError,'变化'):move.apply(plan)
        self.assertIsNone(location.config());self.assertFalse(self.source.is_junction())
        self.assertEqual((self.source/'settings/analysis-store/original.xlsx').read_bytes(),b'new-external-change')
    def test_failed_source_rename_retains_both_copies(self):
        plan=self.prepare();rename=Path.rename
        def fail(path,destination):
            if path==self.source:raise PermissionError('SOURCE-IN-USE')
            return rename(path,destination)
        with patch.object(Path,'rename',fail),self.assertRaisesRegex(ValueError,'Windows拒绝改名'):move.apply(plan)
        self.assertIsNone(location.config());self.assertEqual(self.business(),self.before);self.assertEqual(self.business(self.target),self.before)
    def test_commit_between_copy_verification_and_rename_cannot_be_lost(self):
        plan=self.prepare();rename=Path.rename
        def concurrent_commit(path,destination):
            if path==self.source:
                with closing(sqlite3.connect(self.work.path)) as db,db:db.execute("UPDATE normal_orders SET number='LATEST-COMMITTED'")
            return rename(path,destination)
        with patch.object(Path,'rename',concurrent_commit),self.assertRaisesRegex(ValueError,'变化'):move.apply(plan)
        self.assertIsNone(location.config());self.assertFalse(self.source.is_junction())
        with closing(sqlite3.connect(self.work.path)) as db:self.assertEqual(db.execute('SELECT number FROM normal_orders').fetchone()[0],'LATEST-COMMITTED')
    def test_failed_junction_rolls_back_without_deleting_verified_target(self):
        plan=self.prepare()
        with patch('directory_links.create',side_effect=OSError('LINK-FAILED')),self.assertRaisesRegex(OSError,'LINK-FAILED'):move.apply(plan)
        self.assertEqual(self.business(),self.before);self.assertEqual(self.business(self.target),self.before);self.assertIsNone(location.config())
    def test_failed_pointer_write_restores_original_and_records_first_error(self):
        plan=self.prepare();atomic=move.atomic_json
        def fail(path,value):
            if path==location.config_file():raise OSError('FIRST-POINTER-ERROR')
            return atomic(path,value)
        with patch.object(move,'atomic_json',fail),self.assertRaisesRegex(OSError,'FIRST-POINTER-ERROR'):move.apply(plan)
        self.assertEqual(self.business(),self.before);self.assertFalse(self.source.is_junction())
        self.assertIn('FIRST-POINTER-ERROR',move.status()['error']['traceback'])
    def test_archive_failure_leaves_new_location_and_old_copy_then_retry_works(self):
        plan=self.prepare()
        with patch.object(move,'verify_archive',side_effect=OSError('ARCHIVE-CHECK-FAILED')):result=move.apply(plan)
        self.assertEqual((result['state'],result['phase']),('COMPLETED','CLEANUP_PENDING'))
        self.assertEqual(self.business(self.target),self.before);self.assertTrue(Path(plan['backup']).is_dir())
        result=move.retry_cleanup();self.assertEqual(result['phase'],'COMPLETED');move.verify_archive(plan['archive'],plan)
    def test_unknown_file_in_retired_copy_is_not_deleted(self):
        plan=self.prepare()
        with patch.object(move,'archive_backup',side_effect=OSError('defer cleanup')):move.apply(plan)
        note=Path(plan['backup'])/'new-user-note.txt';note.write_text('DO-NOT-DELETE')
        with self.assertRaisesRegex(ValueError,'变化'):move.retry_cleanup()
        self.assertEqual(note.read_text(),'DO-NOT-DELETE');self.assertEqual(move.status()['state'],'COMPLETED')
        self.assertTrue((Path(plan['backup'])/'settings/workbench.sqlite3').is_file())
    def test_missing_target_disk_never_falls_back_to_empty_store(self):
        self.apply();offline=self.root/'offline';self.target.rename(offline)
        with self.assertRaisesRegex(ValueError,'不会自动切回'):location.root()
        self.assertFalse(self.target.exists());self.assertTrue((offline/'settings/workbench.sqlite3').is_file())
    def test_corrupt_config_fails_closed(self):
        location.control().mkdir(parents=True);location.config_file().write_text('{bad-json')
        with self.assertRaisesRegex(ValueError,'未建立新的空资料库'):location.root()
        self.assertEqual(self.business(),self.before)
    def test_second_move_keeps_all_prior_paths_and_identity(self):
        first_identity=location.identity_path(self.work.path);plan,_=self.apply()
        third=self.root/'第三个位置';second=move.prepare(self.target,third);move.apply(second)
        self.assertEqual(location.root(),third/'settings');self.assertEqual(self.source.resolve(),third.resolve())
        self.assertEqual(location.identity_path(third/'settings/workbench.sqlite3'),first_identity)
        self.assertEqual(len(location.aliases_for(third/'settings')),2);self.assertEqual(self.business(third),self.before)
        self.assertTrue(Path(plan['archive']).is_file());self.assertTrue(Path(second['archive']).is_file())
    def test_wal_commits_are_included_and_staged_material_bytes_unchanged(self):
        original=self.source/'.settings.order-import-old/frozen.sqlite3'
        with closing(sqlite3.connect(original)) as db,db:db.execute('CREATE TABLE original(value)');db.execute("INSERT INTO original VALUES('untouched')")
        staged_hash=move.digest(original)
        # A generated child exits after a committed WAL write, leaving valid WAL
        # material with no live owner. A live owner must remain protected below.
        code="import os,sqlite3,sys; d=sqlite3.connect(sys.argv[1]); d.execute('PRAGMA journal_mode=WAL'); d.execute('CREATE TABLE wal_evidence(value)'); d.execute(\"INSERT INTO wal_evidence VALUES('committed')\"); d.commit(); os._exit(0)"
        result=subprocess.run([sys.executable,'-c',code,str(self.work.path)],capture_output=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
        self.assertTrue(Path(str(self.work.path)+'-wal').exists());plan,result=self.apply()
        with closing(sqlite3.connect(self.target/'settings/workbench.sqlite3')) as db:self.assertEqual(db.execute('SELECT value FROM wal_evidence').fetchone()[0],'committed')
        self.assertEqual(move.digest(self.target/'.settings.order-import-old/frozen.sqlite3'),staged_hash)
    def test_live_wal_handle_is_not_forced_closed_to_make_rename_pass(self):
        with closing(sqlite3.connect(self.work.path)) as owner:
            owner.execute('PRAGMA journal_mode=WAL');owner.execute('SELECT * FROM normal_orders').fetchall()
            with self.assertRaisesRegex(ValueError,'Windows拒绝改名|Windows尚未允许'):self.apply()
            self.assertEqual(owner.execute('SELECT count(*) FROM normal_orders').fetchone()[0],1)
            self.assertFalse(self.source.is_junction());self.assertIsNone(location.config())
    def test_real_interruption_before_pointer_recovers_original(self):
        plan=self.prepare();plan_path=move.plan_file(plan['nonce'])
        code='''import json,os,sys
from pathlib import Path
from unittest.mock import patch
import data_relocation as m
p=json.loads(Path(sys.argv[1]).read_text('utf-8'));rename=Path.rename
def stop(self,destination):
 value=rename(self,destination)
 if self==Path(p['source']):os._exit(73)
 return value
with patch.object(m,'browser_rows',return_value=[]),patch('application_reset._check_other_processes'),patch.object(Path,'rename',stop):m.apply(p)
'''
        run=subprocess.run([sys.executable,'-c',code,str(plan_path)],capture_output=True,timeout=35)
        self.assertEqual(run.returncode,73,run.stderr.decode(errors='replace'));self.assertFalse(self.source.exists())
        result=move.recover();self.assertEqual(result['state'],'FAILED');self.assertEqual(self.business(),self.before)
        self.assertTrue(self.target.is_dir());self.assertIsNone(location.config())
    def test_committed_pointer_interruption_keeps_new_root_and_can_finish_archive(self):
        plan=self.prepare();original=move.archive_backup
        with patch.object(move,'archive_backup',side_effect=OSError('INTERRUPTED-AFTER-COMMIT')):move.apply(plan)
        move.record(plan,'APPLYING','CONFIG_COMMITTED','Simulated interruption after pointer commit')
        value=move.recover();self.assertEqual((value['state'],value['phase']),('COMPLETED','CLEANUP_PENDING'))
        self.assertEqual(location.root(),self.target/'settings');self.assertEqual(self.business(self.target),self.before)
        self.assertEqual(move.retry_cleanup()['phase'],'COMPLETED')
    def test_cancelled_archive_cleanup_keeps_location_and_original_recovery_copy(self):
        import threading
        from runtime_support import Cancelled
        plan=self.prepare()
        with patch.object(move,'archive_backup',side_effect=OSError('defer')):move.apply(plan)
        token=threading.Event();token.set()
        with self.assertRaises(Cancelled):move.retry_cleanup(cancel=token)
        self.assertEqual(move.status()['state'],'COMPLETED');self.assertEqual(move.status()['phase'],'CLEANUP_PENDING')
        self.assertEqual(self.business(self.target),self.before);self.assertTrue(Path(plan['backup']).is_dir())
        self.assertEqual(move.retry_cleanup()['phase'],'COMPLETED')
    def test_archive_is_pinned_before_cleanup_and_a_modified_archive_never_deletes_old_copy(self):
        plan=self.prepare()
        with patch.object(move,'verify_archive',side_effect=OSError('defer')):move.apply(plan)
        pin=move.backup_manifest(plan);metadata=json.loads(pin.read_text('utf-8'))
        metadata['files']['settings/workbench.sqlite3']['sha256']='0'*64;pin.write_text(json.dumps(metadata),'utf-8')
        with self.assertRaisesRegex(ValueError,'变化|校验|不一致'):move.retry_cleanup()
        self.assertTrue((Path(plan['backup'])/'settings/workbench.sqlite3').is_file());self.assertEqual(self.business(self.target),self.before)
    def test_progress_updates_inside_a_single_large_file_and_all_phases(self):
        (self.source/'large.bin').write_bytes(b'x'*(5*1024**2));plan=self.prepare();seen=[];record=move.record
        def capture(*args,**kwargs):
            value=record(*args,**kwargs);seen.append(value);return value
        clock=iter(range(100000))
        with patch.object(move,'record',capture),patch.object(move.time,'monotonic',side_effect=lambda:next(clock)):result=move.apply(plan)
        self.assertEqual(result['phase'],'COMPLETED')
        stages={v['phase'] for v in seen}
        self.assertTrue({'COPYING','CHECKING','SWITCH_RECHECK','ARCHIVE_PRECHECK','ARCHIVING','ARCHIVE_VERIFY','CLEANUP_CHECK','CLEANING_OLD','COMPLETED'}<=stages)
        self.assertTrue(any(v['phase']=='COPYING' and v.get('files')==0 and v.get('bytes_done',0)>0 for v in seen))
        self.assertTrue(any(v['phase']=='ARCHIVING' and v.get('bytes_done',0)>0 for v in seen))
        self.assertEqual(json.loads((location.control()/'operations'/(plan['nonce']+'.status.json')).read_text('utf-8'))['state'],'COMPLETED')
        self.assertEqual(self.business(self.target),self.before)
    def test_file_range_error_retains_bounded_diagnostics_and_source(self):
        plan=self.prepare();original=move.digest;added=False
        def changed(path,progress=None):
            nonlocal added
            result=original(path,progress)
            if not added and Path(path).is_relative_to(Path(plan['stage'])):
                added=True;(self.source/'late-added.txt').write_text('keep')
            return result
        with patch.object(move,'digest',changed),self.assertRaisesRegex(ValueError,'新增文件1'):move.apply(plan)
        self.assertIsNone(location.config());self.assertFalse(self.source.is_junction());self.assertEqual(self.business(),self.before)
        self.assertIn('late-added.txt',move.status()['error']['message'])
    def test_unreadable_subtree_is_not_silently_omitted(self):
        def unreadable(root,**kwargs):
            kwargs['onerror'](PermissionError('SYNTHETIC-UNREADABLE'))
            return iter(())
        with patch.object(move.os,'walk',unreadable),self.assertRaisesRegex(OSError,'无法完整读取'):self.prepare()
        self.assertIsNone(move.status());self.assertEqual(self.business(),self.before)
    def test_closed_clean_wal_database_does_not_mistake_guard_sidecar_for_foreign_change(self):
        path=self.source/'settings/closed-wal.sqlite3'
        with closing(sqlite3.connect(path)) as db,db:
            db.execute('PRAGMA journal_mode=WAL');db.execute('CREATE TABLE facts(value)');db.execute("INSERT INTO facts VALUES('retained')")
        self.assertFalse(Path(str(path)+'-wal').exists())
        plan,result=self.apply();self.assertEqual(result['phase'],'COMPLETED');move.verify_archive(plan['archive'],plan)
        self.assertFalse(result['result']['old_copy_remains']);self.assertTrue(result['result']['retained_technical_directory'])
        with closing(sqlite3.connect(self.target/'settings/closed-wal.sqlite3')) as db:self.assertEqual(db.execute('SELECT value FROM facts').fetchone()[0],'retained')
        self.assertEqual(self.business(self.target),self.before)
    def test_foreign_file_added_while_guards_open_still_blocks_switch(self):
        plan=self.prepare();original=move.active_database;changed=False
        def external_change(relative,path):
            nonlocal changed
            if not changed:
                changed=True;(self.source/'foreign-added.txt').write_text('KEEP-FOREIGN')
            return original(relative,path)
        with patch.object(move,'active_database',external_change),self.assertRaisesRegex(ValueError,'外部变化'):move.apply(plan)
        self.assertEqual((self.source/'foreign-added.txt').read_text(),'KEEP-FOREIGN');self.assertIsNone(location.config());self.assertEqual(self.business(),self.before)
    def test_inherited_working_directory_is_released_before_copy(self):
        prior=Path.cwd();plan=self.prepare();os.chdir(self.source/'settings')
        try:
            result=move.apply(plan);self.assertEqual(result['phase'],'COMPLETED');self.assertEqual(Path.cwd(),location.control())
            self.assertEqual(self.business(self.target),self.before)
        finally:os.chdir(prior)
    def test_program_installed_in_data_is_rejected_before_copy(self):
        with patch.object(sys,'frozen',True,create=True),patch.object(sys,'executable',str(self.source/'program.exe')),self.assertRaisesRegex(ValueError,'程序文件本身'):self.prepare()
        self.assertIsNone(move.status());self.assertFalse(self.target.exists())
    def test_real_external_directory_handle_blocks_before_expensive_copy(self):
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE];kernel.CreateFileW.restype=wintypes.HANDLE
        kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.GetFileType.argtypes=[wintypes.HANDLE]
        plan=self.prepare();handle=kernel.CreateFileW(str(self.source),1,3,None,3,0x02000000,None);self.assertNotEqual(handle,ctypes.c_void_p(-1).value)
        try:
            with self.assertRaisesRegex(ValueError,'Windows尚未允许'):move.apply(plan)
            self.assertEqual(kernel.GetFileType(handle),1);self.assertFalse(Path(plan['stage']).exists());self.assertEqual(self.business(),self.before)
        finally:kernel.CloseHandle(handle)

if __name__=='__main__':unittest.main()
