import hashlib,json,os,sqlite3,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import closing
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tools')]
from prepare_collection_maintenance import seed
from aoxiang_jobs import Jobs
from aoxiang_acquisition import prepare,manifest
from aoxiang_client import ProcessClient,ComponentError

class MaintenanceProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.out=Path(self.tmp.name)/'qa';self.fixture=seed(self.out)
        self.jobs=Jobs(self.out/'settings/aoxiang/jobs.sqlite3')
        self.client=ProcessClient(ROOT/'components/aoxiang',Path(self.fixture['state_root']));self.client.start()
    def tearDown(self):
        self.assertTrue(self.client.close());self.tmp.cleanup()
    def test_actual_protocol_end_delete_new_and_business_unchanged(self):
        original=self.jobs.get('adopted');value=manifest(original,self.fixture['state_root'])
        files=[Path(self.fixture['manifest']),*[Path(p) for p in self.fixture['command_files']]]
        before={str(p):p.read_bytes() for p in files}
        import normal_orders,work_repository,normal_exports
        from jst_results_smoke import cells
        work=work_repository.Repository(self.out/'settings');orders=normal_orders.Repository(work)
        signed=orders.get(work_repository.today(),'T');draft=work.get_job(work_repository.today())
        normal_exports.export(signed,self.out/'signed-before.xlsx',warehouse='河北仓')
        signed_cells=cells(self.out/'signed-before.xlsx')
        config=self.client.request('configuration')
        with self.assertRaisesRegex(ValueError,'不能另建'):
            prepare(self.jobs,config,['W2','W1'],'refresh',day='2026-11-04')
        plan=self.jobs.maintenance_preview(self.client,'blocked','end')
        self.assertEqual(len(plan['related']),1)
        self.jobs.maintenance_apply(self.client,plan)
        self.assertEqual(self.jobs.get('old-failed')['state'],'ENDED')
        self.assertEqual(self.jobs.unfinished(),[])
        job=prepare(self.jobs,config,['W2','W1'],'refresh',day='2026-11-04')
        self.jobs.dispatch(self.client,job['id'])
        until=time.monotonic()+6
        while time.monotonic()<until:
            job=self.jobs.refresh(self.client,job['id'])
            if job['state']=='INTERRUPTED':break
            time.sleep(.01)
        # No browser binding in fixture: proves scheduler progressed past original task gates.
        self.assertEqual(job['record']['error']['code'],'SETUP_REQUIRED')
        self.jobs.maintenance_apply(self.client,self.jobs.maintenance_preview(self.client,job['id'],'end'))
        for ident in ['blocked','old-failed','adopted',job['id']]:
            self.jobs.maintenance_apply(self.client,self.jobs.maintenance_preview(self.client,ident,'delete'))
        self.assertEqual(self.jobs.recent(),[])
        self.assertEqual(before,{str(p):p.read_bytes() for p in files})
        self.assertEqual(manifest(original,self.fixture['state_root']),value)
        self.assertEqual(work.get_job(work_repository.today()),draft)
        self.assertEqual(orders.get(work_repository.today(),'T'),signed)
        normal_exports.export(orders.get(work_repository.today(),'T'),self.out/'signed-after.xlsx',warehouse='河北仓')
        self.assertEqual(cells(self.out/'signed-after.xlsx'),signed_cells)
        with closing(sqlite3.connect(self.fixture['business_db'])) as db:after='\n'.join(db.iterdump())
        self.assertEqual(after,(self.out/'business-before.sql').read_text('utf-8'))
    def test_lost_end_and_delete_responses_reconciled_after_restart(self):
        p=self.jobs.maintenance_preview(self.client,'blocked','end')
        self.client.request('collection.apply',command_id=p['id'],operation='end',token=p['token'])
        self.assertEqual(self.jobs.get('blocked')['state'],'NEEDS_REVIEW')
        self.jobs.synchronize(self.client);self.assertEqual(self.jobs.get('blocked')['state'],'ENDED')
        p=self.jobs.maintenance_preview(self.client,'blocked','delete')
        self.client.request('collection.apply',command_id=p['id'],operation='delete',token=p['token'])
        self.jobs.synchronize(self.client)
        with self.assertRaisesRegex(ValueError,'未找到'):self.jobs.get('blocked')
    def test_local_unaccepted_end_and_delete(self):
        conf=self.client.request('configuration');job=prepare(self.jobs,conf,['W3'],'reuse',day='2026-11-04')
        self.jobs.maintenance_apply(self.client,self.jobs.maintenance_preview(self.client,job['id'],'end'))
        self.assertEqual(self.jobs.get(job['id'])['state'],'CANCELLED')
        self.jobs.maintenance_apply(self.client,self.jobs.maintenance_preview(self.client,job['id'],'delete'))
        with self.assertRaises(ValueError):self.jobs.get(job['id'])

class MaintenanceUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        from aoxiang_fetch_ui import FetchDialog
        self.tmp=tempfile.TemporaryDirectory();self.out=Path(self.tmp.name)/'qa';self.fixture=seed(self.out)
        self.errors=[];self.error_patch=patch('readable_ui.failure',side_effect=lambda *args:self.errors.append(args[-1]));self.error_patch.start()
        self.d=FetchDialog(None,self.out/'settings','2026-11-04');self.d.show();self.wait(lambda:self.d.worker is None)
    def wait(self,predicate):
        from PySide6.QtTest import QTest
        until=time.monotonic()+8
        while not predicate() and time.monotonic()<until:self.app.processEvents();QTest.qWait(10)
        self.assertTrue(predicate())
    def tearDown(self):
        import shiboken6
        self.d.close();self.wait(lambda:self.d._can_close and self.d.worker is None);shiboken6.delete(self.d)
        self.error_patch.stop();self.tmp.cleanup()
    def test_cancel_end_confirm_delete_and_reopen_scope(self):
        d=self.d;self.assertEqual(d.current['id'],'blocked');self.assertTrue(d.end_btn.isEnabled());self.assertFalse(d.delete_btn.isEnabled())
        with patch.object(d,'confirm_maintenance',return_value=False):
            d.end_btn.click();self.wait(lambda:d.worker is None)
        self.assertEqual(d.current['state'],'NEEDS_REVIEW')
        with patch.object(d,'confirm_maintenance',return_value=True):
            d.end_btn.click();self.wait(lambda:d.worker is None and d.current['state']=='ENDED')
        self.assertTrue(d.delete_btn.isEnabled());self.assertFalse(d.end_btn.isEnabled())
        with patch.object(d,'confirm_maintenance',return_value=False):
            d.delete_btn.click();self.wait(lambda:d.worker is None)
        self.assertEqual(d.current['id'],'blocked')
        with patch.object(d,'confirm_maintenance',return_value=True):
            d.delete_btn.click();self.wait(lambda:d.worker is None and d.history.findData('blocked')<0)
        self.assertEqual(d.history.count(),2)
        d.select_visible();d.start_btn.click();self.wait(lambda:d.worker is None and d.current['id'].startswith('rep-'))
        self.assertNotIn('不能另建任务',' '.join(self.errors));self.assertFalse(self.errors)

    def test_component_exit_does_not_disable_end_and_new_collection(self):
        d=self.d
        self.assertTrue(d.client.close())
        with patch.object(d,'confirm_maintenance',return_value=True):
            d.end_btn.click();self.wait(lambda:d.worker is None and d.current['state']=='ENDED')
        self.assertTrue(d.client.close())
        d.select_visible();d.start_btn.click()
        self.wait(lambda:d.worker is None and d.current['id'].startswith('rep-'))
        self.assertFalse(self.errors)

if __name__=='__main__':unittest.main()
