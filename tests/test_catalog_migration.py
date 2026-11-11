"""Migrated catalog tasks survive without moving a browser or target old tasks."""
import copy,hashlib,json,sqlite3,unittest,zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from stockout_common import atomic_json,file_hash
import order_migration as migration
import catalog_migration as catalog
import test_order_migration as seed


class CatalogMigration(unittest.TestCase):
    def setUp(self):
        self.f=seed.MigrationTests();self.f.setUp();self.addCleanup(self.f.tmp.cleanup)
        self.source=self.f.source;self.target=self.f.target
        self.account={'id':'source-sd','warehouse':'山东仓','expectedCompany':'测试公司','expectedCoid':'11','expectedUid':'22',
                      'mappingVerified':True,'bindingVersion':7,'enabled':True,'binding':{'actualProfilePath':'SOURCE-SECRET-PROFILE'}}
        self.source_state=self.state(self.source,'SOURCE-ORIGINAL',self.account)
        self.target_account={**self.account,'bindingVersion':2,'binding':{'actualProfilePath':'TARGET-LOCAL-PROFILE'}}
        self.target_state=self.state(self.target,'TARGET-OLD',self.target_account)
    def state(self,root,bid,account):
        folder=root/catalog.ROOT;file=folder/'downloads'/bid/'one'/'native.xlsx';file.parent.mkdir(parents=True);file.write_bytes(b'original-task-file')
        sha=file_hash(file)
        data={'schemaVersion':1,'storeId':bid+'-identity','assets':{},'accounts':{'山东仓':account},
              'operations':{'old-probe':{'state':'SUCCEEDED','result':{'binding':{'actualProfilePath':'SOURCE-SECRET-PROFILE'}}}},
              'batches':{bid:{'id':bid,'createdAt':'2026-11-11','purpose':'default_catalog','state':'RUNNING','operation_id':'OP-'+bid,
                  'items':[{'id':'one','account':account,'stage':'DOWNLOADED','native':{'stage':'DOWNLOADED','submissions':1,'nativeTaskRef':'TASK-UNIQUE-'+bid,'file':str(file),'sha256':sha},
                      'receipt':{'source':str(file),'source_sha256':sha,'candidate':str(file),'candidate_sha256':sha,'warehouse':'山东仓'},'adoption':None}]}}}
        atomic_json(folder/'jobs.json',data);atomic_json(folder/'store.guard.json',{'schemaVersion':1,'storeId':data['storeId']})
        with closing(sqlite3.connect(folder/'writer.lock.sqlite')) as db,db:db.execute('CREATE TABLE guard(v)')
        return data
    def prepare(self):
        migration.export(self.source,self.f.package);return migration.prepare(self.target,self.f.package)
    def apply(self,plan):
        with patch('application_reset._check_other_processes'):return migration.apply(plan)
    def test_export_is_readonly_and_contains_tasks_files_without_source_binding(self):
        before=seed.files(self.source);migration.export(self.source,self.f.package);self.assertEqual(seed.files(self.source),before)
        with zipfile.ZipFile(self.f.package) as book:
            data=json.loads(book.read(catalog.ROOT+'/jobs.json'))
            self.assertNotIn(b'SOURCE-SECRET-PROFILE',book.read(catalog.ROOT+'/jobs.json'))
            self.assertEqual(data['accounts'],{});self.assertEqual(data['operations'],{})
            item=data['batches']['SOURCE-ORIGINAL']['items'][0]
            self.assertEqual(item['native']['nativeTaskRef'],'TASK-UNIQUE-SOURCE-ORIGINAL');self.assertNotIn('binding',item['account'])
            self.assertEqual(book.read(catalog.ROOT+'/downloads/SOURCE-ORIGINAL/one/native.xlsx'),b'original-task-file')
    def test_restore_replaces_target_tasks_keeps_local_binding_and_translates_file_paths(self):
        before=seed.files(self.target);plan=self.prepare();result=self.apply(plan)
        self.assertEqual(seed.files(Path(result['backup'])),before)
        data=json.loads((self.target/catalog.ROOT/'jobs.json').read_text('utf-8'))
        self.assertEqual(set(data['batches']),{'SOURCE-ORIGINAL'});self.assertEqual(data['accounts']['山东仓']['binding'],self.target_account['binding'])
        self.assertFalse(data['accounts']['山东仓']['enabled']);self.assertTrue(data['rebindRequired']['山东仓'])
        b=data['batches']['SOURCE-ORIGINAL'];i=b['items'][0]
        self.assertEqual(b['state'],'PAUSED');self.assertTrue(i['migration']['needs_binding']);self.assertEqual(i['native']['submissions'],1)
        expected=self.target/catalog.ROOT/'downloads/SOURCE-ORIGINAL/one/native.xlsx'
        self.assertEqual(i['native']['file'],str(expected));self.assertEqual(i['receipt']['source'],str(expected));self.assertEqual(file_hash(expected),i['native']['sha256'])
    def test_legacy_package_clears_target_task_area_but_keeps_whole_backup(self):
        folder=self.source/catalog.ROOT
        # Build an older-format archive without deleting the fixture source.
        original=migration.tree
        with patch.object(migration,'tree',side_effect=lambda root:[p for p in original(root) if not p.is_relative_to(Path(root)/catalog.ROOT)]),patch.object(catalog,'freeze',return_value=None):
            migration.export(self.source,self.f.package)
        plan=migration.prepare(self.target,self.f.package);before=seed.files(self.target);result=self.apply(plan)
        self.assertEqual(seed.files(Path(result['backup'])),before)
        data=json.loads((self.target/catalog.ROOT/'jobs.json').read_text('utf-8'));self.assertEqual(data['batches'],{})
        self.assertEqual(data['accounts']['山东仓']['binding'],self.target_account['binding'])
    def test_active_source_writer_requires_rpc_snapshot_without_mutation(self):
        folder=self.source/catalog.ROOT
        with closing(sqlite3.connect(folder/'writer.lock.sqlite',isolation_level=None)) as db:
            db.execute('BEGIN EXCLUSIVE')
            with self.assertRaisesRegex(ValueError,'任务锁'):migration.export(self.source,self.f.package)
            self.assertFalse(self.f.package.exists())
            migration.export(self.source,self.f.package,catalog_state=self.source_state)
        self.assertTrue(self.f.package.exists())
    def test_active_target_component_or_bad_guard_prevents_any_replacement(self):
        plan=self.prepare();before=seed.files(self.target)
        with closing(sqlite3.connect(self.target/catalog.ROOT/'writer.lock.sqlite',isolation_level=None)) as db:
            db.execute('BEGIN EXCLUSIVE')
            with self.assertRaisesRegex(ValueError,'采集服务'):self.apply(plan)
        self.assertEqual(seed.files(self.target),before);self.assertFalse(Path(plan['backup']).exists())
        atomic_json(self.target/catalog.ROOT/'store.guard.json',{'schemaVersion':1,'storeId':'wrong'})
        with self.assertRaisesRegex(ValueError,'身份'):self.apply(plan)
        self.assertFalse(Path(plan['backup']).exists())
    def test_second_export_contains_no_target_connection_and_keeps_native_identity(self):
        self.apply(self.prepare());second=self.f.root/'second-catalog.zip';migration.export(self.target,second)
        with zipfile.ZipFile(second) as book:
            raw=book.read(catalog.ROOT+'/jobs.json');self.assertNotIn(b'TARGET-LOCAL-PROFILE',raw)
            self.assertEqual(json.loads(raw)['batches']['SOURCE-ORIGINAL']['items'][0]['native']['nativeTaskRef'],'TASK-UNIQUE-SOURCE-ORIGINAL')
