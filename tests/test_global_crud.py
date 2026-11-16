"""Isolated regressions for cross-workbench identity, dispatch and read boundaries."""
import copy,json,os,sqlite3,sys,tempfile,threading,unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import work_repository as wr


class DirectoryTests(unittest.TestCase):
    def test_setup_only_new_store_and_legacy_fallback(self):
        import openpyxl,payment_shop_names as names,admission_overview as admission,replenishment_plans as plans
        from aoxiang_directory import Directory,read_directory,load_for_settings
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);repo=wr.Repository(root/'settings');p=root/'directory.xlsx'
            w=openpyxl.Workbook();w.active.append(['门店编码','门店名称']);w.active.append(['NEW','目录新店']);w.save(p);w.close()
            Directory(repo.path.parent).save(read_directory(p))
            self.assertEqual(load_for_settings(repo.path.parent)['items'][0]['code'],'NEW')
            canonical=Directory(repo.path.parent/'aoxiang');canonical.save(read_directory(p))
            for actual in (plans.available(SimpleNamespace(work_repository=repo,loaded=None)),names.load(repo.path)['names'],admission.load(repo.path)['names']):
                self.assertEqual(actual['NEW'],'目录新店')
            plan=names.prepare(names.load(repo.path),[{'store':'NEW','warehouse':'山东仓','value':'聚水潭新店'}]);names.save(repo.path,plan)
            canonical.path.write_text('{bad','utf-8')
            with self.assertRaises(ValueError):load_for_settings(repo.path.parent)


class DispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        from PySide6.QtWidgets import QWidget,QStackedWidget
        from stockout_entry import Entry
        self.host=QWidget();self.host.sections=QStackedWidget(self.host);self.host.worker=object();self.host.closing=False
        self.entry=Entry(self.host,navigation=False,schedule=False)
        self.entry.page=SimpleNamespace(note=SimpleNamespace(setText=lambda _:None),controls=lambda:None)
        self.sent=[]
        def begin(r):
            self.sent.append((r[0],r[1]));self.entry.worker=SimpleNamespace(operation=r[0],cancel=threading.Event(),cancel_reason='')
        self.entry.begin=begin
        self.addCleanup(self.cleanup)
    def cleanup(self):
        self.entry.worker=None;self.entry.prepare_close();self.host.deleteLater();self.app.processEvents()
    def test_query_can_run_with_main_calculation_but_not_migration(self):
        self.entry.start('query');self.assertEqual(self.sent,[('query',{})])
        self.entry.worker=None;self.host._data_location_preparing=True;self.entry.start('query',{'x':1})
        self.assertEqual(len(self.sent),1);self.assertIsNotNone(self.entry.queued)
    def test_live_configuration_keeps_platform_gate(self):
        self.entry.start('choices');self.assertEqual(self.sent,[]);self.assertEqual(self.entry.queued[0],'choices')
        self.entry.pause();self.assertNotIn('未保存',self.entry.message)
    def test_pending_writes_survive_queries_in_order(self):
        args={'views':['original']};self.entry.start('save_views',args);args['views'].append('later edit')
        self.entry.start('query',{'page':1});self.entry.start('query',{'page':2});self.entry.start('save_views',{'views':['second']});self.entry.start('query',{'page':3})
        self.assertEqual(len(self.entry._requests),4);self.host.worker=None
        for _ in range(4):self.entry.worker=None;self.entry.tick()
        self.assertEqual(self.sent,[('save_views',{'views':['original']}),('query',{'page':2}),('save_views',{'views':['second']}),('query',{'page':3})])
        self.assertIsNone(self.entry.queued)
    def test_cancellation_reports_unexecuted_write(self):
        self.entry.start('save_views',{'views':[]});self.entry.pause()
        self.assertIsNone(self.entry.queued);self.assertIn('未保存',self.entry.message);self.assertEqual(self.sent,[])
    def test_query_does_not_replace_metadata_or_delete_preview(self):
        self.host._data_location_preparing=True
        for operation in ('open','delete_preview','query','query'):self.entry.start(operation)
        self.assertEqual([r[0] for r in self.entry._requests],['open','delete_preview','query'])
    def test_auto_yield_does_not_discard_pending_write(self):
        self.entry.worker=SimpleNamespace(operation='collect',cancel=threading.Event(),cancel_reason='')
        self.entry.start('save_views',{});self.entry.tick()
        self.assertTrue(self.entry.worker.cancel.is_set());self.assertEqual(self.entry.queued[0],'save_views')
    def test_history_initial_read_can_be_cancelled_without_error_window(self):
        from runtime_support import Cancelled
        from settings_maintenance_ui import open_history
        with tempfile.TemporaryDirectory() as temp:
            self.host.work_repository=wr.Repository(temp)
            with patch('business_ui.run',side_effect=Cancelled('cancelled')):self.assertIsNone(open_history(self.host,self.host))


class HistoryTests(unittest.TestCase):
    def test_paged_metadata_lazy_detail_and_stable_cursor(self):
        import settings_history as h
        with tempfile.TemporaryDirectory() as temp:
            repo=wr.Repository(temp)
            with closing(repo.connect()) as db,db:
                for n in range(57):h.record(db,{'S':n},{'S':n+1},str(n))
            with patch.object(wr,'loads',side_effect=AssertionError('Listing must not decode full payload')):
                first=h.page(repo);self.assertEqual(len(first['items']),50);self.assertEqual(first['items'][0]['action'],'56')
                second=h.page(repo,before=first['next']);self.assertEqual(len(second['items']),7);self.assertIsNone(second['next'])
            selected=h.entry(repo,first['items'][0]['id']);self.assertEqual(selected['before'],{'S':56})
            with closing(repo.connect()) as db,db:h.record(db,{'S':57},{'S':58},'new')
            self.assertEqual(h.page(repo,before=first['next']),second)


class SchedulerTests(unittest.TestCase):
    def test_idle_poll_has_no_write_reservation(self):
        import scheduled_replenishment as s
        with tempfile.TemporaryDirectory() as temp:
            repo=wr.Repository(temp);scheduler=s.Repository(repo.path);scheduler.ensure();calls=[];original=sqlite3.connect
            def connect(*a,**kw):
                db=original(*a,**kw);db.set_trace_callback(calls.append);return db
            with patch.object(sqlite3,'connect',connect):self.assertEqual(scheduler.due(),[])
            self.assertFalse(any(sql.startswith('BEGIN IMMEDIATE') for sql in calls))

if __name__=='__main__':unittest.main()
