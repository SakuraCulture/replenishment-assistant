"""Failure recovery must preserve signing, identities and user-edited files."""
import errno
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import export_receipts as receipts
import export_recovery as recovery
import export_files
import normal_delivery
import lifecycle_batch
import lifecycle_diagnostics
import archive_batch
import test_lifecycle as fixture
from test_archive_batch import fixture as archive_fixture


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.LifecycleTests();self.f.setUp()
        import work_repository
        self.order=self.f.repo.get(work_repository.today(),'T')
        self.target=self.f.root/'signed.xlsx'
    def tearDown(self):self.f.tearDown()
    def test_log_failure_preserves_file_and_recovers_once_without_signing(self):
        before=self.f.repo.headers()
        with patch.object(self.f.repo,'log',side_effect=OSError('isolated log failure')):
            with self.assertRaisesRegex(RuntimeError,'文件已生成，日志写入失败'):
                normal_delivery.export_signed(self.f.repo,self.order,self.target,['河北仓'])
        task=receipts.recent(self.f.path)[0];self.assertEqual(task['files'][0]['state'],'已生成')
        review=recovery.inspect(self.f.path,task['id']);self.assertEqual(review['items'][0]['action'],'record')
        checksum=export_files.sha256(self.target)
        recovery.recover(self.f.path,review,[0],self.f.root)
        self.assertEqual(recovery.inspect(self.f.path,task['id'])['items'][0]['action'],'keep')
        self.assertEqual(self.f.repo.headers(),before);self.assertEqual(export_files.sha256(self.target),checksum)
        self.assertEqual(len([e for e in self.f.repo.events() if e['action']=='带号导出']),1)
    def test_changed_file_is_kept_and_recovery_uses_original_revision(self):
        normal_delivery.export_signed(self.f.repo,self.order,self.target,['河北仓'])
        task=receipts.recent(self.f.path)[0];self.target.write_bytes(b'user edited original')
        self.f.second_revision()
        review=recovery.inspect(self.f.path,task['id']);self.assertEqual(review['items'][0]['action'],'export')
        result=recovery.recover(self.f.path,review,[0],self.f.root)
        self.assertEqual(self.target.read_bytes(),b'user edited original')
        import openpyxl,order_identity
        book=openpyxl.load_workbook(result['paths'][0],read_only=True)
        self.assertEqual(order_identity.read(book)[0]['revision'],1);book.close()
        self.assertEqual(self.f.repo.headers()[0]['revision'],2)
    def test_unknown_commit_does_not_guess_a_revision(self):
        task=receipts.begin(self.f.path,'merchant',[{'path':str(self.target),'state':'未执行','stage':'准备','head':None}],signature='签发结果待核')
        review=recovery.inspect(self.f.path,task['id']);self.assertEqual(review['items'][0]['action'],'blocked')
        with self.assertRaisesRegex(ValueError,'原修订待核'):recovery.recover(self.f.path,review,[0],self.f.root)
        self.assertFalse(self.target.exists())
    def test_no_clobber_publication(self):
        source=self.f.root/'.temporary';source.write_bytes(b'new');self.target.write_bytes(b'old')
        with self.assertRaises(FileExistsError):export_files.publish_new(source,self.target)
        self.assertEqual(self.target.read_bytes(),b'old');self.assertEqual(source.read_bytes(),b'new')
    def test_old_return_explains_stale_revision_without_selecting_it(self):
        normal_delivery.export_signed(self.f.repo,self.order,self.target,['河北仓'])
        document=lifecycle_batch.read([self.target],'return')['documents'][0]
        self.f.second_revision()
        import order_lifecycle
        result=lifecycle_diagnostics.binding(document,order_lifecycle.current_targets(self.f.path))
        self.assertEqual(result['candidates'],[]);self.assertFalse(result['automatic']);self.assertIn('原签发已变化',result['reason'])
    def test_missing_identity_does_not_choose_by_similar_goods(self):
        document={'scan':{'rows':[{'sku':'0001','store':'T','warehouse':'河北仓'}]}}
        result=lifecycle_diagnostics.binding(document,[self.f.h])
        self.assertEqual(len(result['candidates']),1);self.assertFalse(result['automatic'])
        self.assertTrue(lifecycle_diagnostics.binding(document,[self.f.h],scoped=True)['automatic'])
    def test_manifest_failure_does_not_hide_completed_archive_files(self):
        work,repo,heads,_=archive_fixture(self.f.root/'other')
        plan=archive_batch.prepare(work.path,heads,'signed','combined')
        write_text=Path.write_text
        def fail_manifest(path,*args,**kwargs):
            if path.name=='导出清单.txt':raise OSError(errno.ENOSPC,'isolated full disk')
            return write_text(path,*args,**kwargs)
        with patch.object(Path,'write_text',fail_manifest):result=archive_batch.execute(work.path,plan,self.f.root)
        self.assertEqual(len(result['files']),2);self.assertEqual(result['failed'],[])
        self.assertIn('清单写入失败',result['warnings'][0])
        self.assertTrue(all(Path(f['path']).is_file() for f in result['files']))


if __name__=='__main__':unittest.main()
