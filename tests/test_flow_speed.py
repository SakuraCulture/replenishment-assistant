"""Bounded work, original concurrency checks, and unchanged Excel contracts."""
import copy,io,json,sqlite3,sys,threading,unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).parent)]
import openpyxl
import order_lifecycle as lc,lifecycle_batch as batch,purchases
import order_analytics as analytics,order_analytics_exports as exports
import analytics_template_export as layout
from analytics_export_validation import SnapshotCheck
from runtime_support import Cancelled
import test_lifecycle as lifecycle_fixture
import test_order_analytics as analytics_fixture


class LifecycleBoundaries(unittest.TestCase):
    def setUp(self):self.f=lifecycle_fixture.LifecycleTests();self.f.setUp()
    def tearDown(self):self.f.tearDown()
    def test_queries_follow_documents_not_number_of_sku_rows(self):
        queries=[];connect=sqlite3.connect
        def observed(*a,**kw):
            db=connect(*a,**kw);db.set_trace_callback(queries.append);return db
        scan=self.f.scan(items=[{} for _ in range(1000)]);before=copy.deepcopy(scan)
        with patch.object(sqlite3,'connect',observed):p=batch.prepare(self.f.path,batch.group_scan(scan))
        self.assertLessEqual(sum(s=='PRAGMA data_version' for s in queries),2)
        self.assertEqual(scan,before);self.assertEqual(p['plans'][0]['duplicates'],999)
        result=lc.commit(self.f.path,p);self.assertEqual(len(result),1)
    def test_changed_head_during_row_grouping_still_rejects_at_plan_boundary(self):
        scan=self.f.scan(items=[{},{}]);head=self.f.h;original=purchases.validate_head;calls=[]
        def changed(db,h):
            value=original(db,h);calls.append(1)
            if len(calls)==1:
                with closing(sqlite3.connect(self.f.path)) as writer,writer:
                    writer.execute('UPDATE normal_orders SET current=current+1 WHERE oid=?',(head['oid'],))
            return value
        with patch.object(purchases,'validate_head',changed):
            with self.assertRaises(ValueError):lc.prepare(self.f.path,scan,{lc.binding_key(r):head for r in scan['rows']})
        self.assertGreaterEqual(len(calls),1)
    def test_foreign_writer_can_commit_inside_cached_row_loop(self):
        scan=self.f.scan(items=[{} for _ in range(20)]);path=self.f.path
        class Writer:
            count=0
            def is_set(s):
                s.count+=1
                if s.count==2:
                    with closing(sqlite3.connect(path,timeout=.1)) as db,db:
                        db.execute('CREATE TABLE unrelated_progress(value)')
                return False
        p=lc.prepare(self.f.path,scan,{lc.binding_key(r):self.f.h for r in scan['rows']},cancel=Writer())
        self.assertEqual(p['plans'][0]['duplicates'],19)
    def test_deleted_source_after_preview_cannot_be_committed(self):
        p=lc.prepare_numbered(self.f.path,self.f.scan())
        with closing(sqlite3.connect(self.f.path)) as db,db:
            db.execute('DELETE FROM normal_orders WHERE oid=?',(self.f.h['oid'],))
        with self.assertRaises(ValueError):lc.commit(self.f.path,p)


class ExportChecks(unittest.TestCase):
    def setUp(self):
        self.f=analytics_fixture.AnalyticsTests();self.f.setUp();self.path=self.f.path;self.report=self.f.report()
    def tearDown(self):self.f.tearDown()
    def test_one_full_check_per_unchanged_export_and_no_cross_export_cache(self):
        for level in ('analysis','analysis_detail'):
            with patch.object(analytics,'collect',wraps=analytics.collect) as collect:
                exports.export(self.path,self.report,self.f.root/(level+'.xlsx'),level=level)
            self.assertEqual(collect.call_count,1,level)
    def test_unrelated_commit_uses_full_fallback_without_holding_reader_lock(self):
        with SnapshotCheck(self.path,self.report) as check,patch.object(analytics,'collect',wraps=analytics.collect) as collect:
            self.assertTrue(check());self.assertFalse(check.db.in_transaction)
            with closing(sqlite3.connect(self.path,timeout=.1)) as writer,writer:
                writer.execute('CREATE TABLE unrelated_task_progress(value)')
            self.assertTrue(check());self.assertEqual(collect.call_count,2)
        self.assertIsNone(check.db)
    def test_changed_payment_or_source_is_not_hidden_by_proof(self):
        with SnapshotCheck(self.path,self.report) as check:
            check();self.f.f.save('return')
            with self.assertRaisesRegex(ValueError,'变化'):check()
    def test_uncertain_fence_keeps_original_full_checks(self):
        with SnapshotCheck(self.path,self.report) as check,patch.object(check,'_fence',return_value=None),patch.object(analytics,'collect',wraps=analytics.collect) as collect:
            self.assertTrue(check());self.assertTrue(check());self.assertEqual(collect.call_count,2)
    def test_unavailable_file_identity_does_not_create_a_new_export_gate(self):
        with patch('analytics_export_validation._file_id',side_effect=OSError('identity unavailable')):
            with SnapshotCheck(self.path,self.report) as check,patch.object(analytics,'collect',wraps=analytics.collect) as collect:
                self.assertTrue(check());self.assertTrue(check());self.assertEqual(collect.call_count,2)
    def test_historical_query_keeps_full_validator_decision_after_future_commit(self):
        from datetime import datetime,timedelta,timezone
        now=datetime.now(timezone(timedelta(hours=8)))
        report=self.f.report(as_of=now.isoformat())
        with SnapshotCheck(self.path,report) as check:
            check()
            with patch('work_repository.now',return_value=(now+timedelta(days=1)).isoformat()):self.f.f.save('return')
            self.assertTrue(analytics.validate_snapshot(self.path,report))
            self.assertTrue(check())
    def test_changed_query_cannot_reuse_previous_validation(self):
        with SnapshotCheck(self.path,self.report) as check:
            check();self.report['query']['orders']=['missing-order']
            with self.assertRaisesRegex(ValueError,'不存在'):check()
    def test_cancelled_after_full_validation_is_still_cancelled(self):
        stop=threading.Event()
        with SnapshotCheck(self.path,self.report,cancel=stop) as check:
            check();stop.set()
            with self.assertRaises(Cancelled):check()
    def test_late_source_change_does_not_publish_and_writer_is_not_blocked(self):
        original=layout.append_detail;target=self.f.root/'late.xlsx'
        def changed(*a,**kw):
            result=original(*a,**kw);self.f.f.save('return');return result
        with patch.object(layout,'append_detail',changed):
            with self.assertRaisesRegex(ValueError,'变化'):exports.export(self.path,self.report,target,level='analysis_detail')
        self.assertFalse(target.exists());self.assertFalse(list(self.f.root.glob('.analytics-*')))
    def test_external_component_change_without_main_commit_still_rejects(self):
        from test_analytics_identifiers import seed_purchase
        self.f.f.save('return');self.f.f.save('payment')
        h=self.f.f.h;payment=lc.snapshot(self.path,h)['documents']['payment']
        file,_=seed_purchase(self.path,h,payment);report=self.f.report()
        with SnapshotCheck(self.path,report) as check:
            check();before=check.db.execute('PRAGMA data_version').fetchone()[0]
            with closing(sqlite3.connect(file)) as db,db:
                db.execute('INSERT INTO purchase_receipts VALUES(?,?)',('purchase-one',json.dumps({'state':'VOIDED','summary':{'status':'已作废'}})))
            self.assertEqual(check.db.execute('PRAGMA data_version').fetchone()[0],before)
            with self.assertRaisesRegex(ValueError,'变化'):check()
    def test_legacy_component_file_corruption_is_still_fully_rejected(self):
        from test_analytics_identifiers import seed_purchase
        from aoxiang_purchase import fingerprint
        self.f.f.save('return');self.f.f.save('payment');h=self.f.f.h
        payment=lc.snapshot(self.path,h)['documents']['payment'];plan={'head':copy.deepcopy(h),'rows':[]}
        file=self.path.parent/'aoxiang/purchase-packages/purchase-one/付款采购依据.json';file.parent.mkdir(parents=True)
        file.write_text(json.dumps(plan,ensure_ascii=False),'utf-8')
        seed_purchase(self.path,h,payment,basis_changes={'oid':None,'planFingerprint':fingerprint(plan)})
        report=self.f.report()
        with SnapshotCheck(self.path,report) as check:
            check();plan['rows'].append({'sku':'changed'})
            file.write_text(json.dumps(plan,ensure_ascii=False),'utf-8')
            with self.assertRaisesRegex(ValueError,'摘要不符'):check()
    def test_old_proof_cannot_hide_corrupt_archive_bytes(self):
        with SnapshotCheck(self.path,self.report) as check:
            check()
            with closing(sqlite3.connect(self.path)) as db,db:
                db.execute('UPDATE normal_revisions SET payload=?',(sqlite3.Binary(b'not-valid-json'),))
            with self.assertRaises(ValueError):check()


class WorkbookStyles(unittest.TestCase):
    def test_styles_do_not_leak_across_workbooks_formats_or_text_kinds(self):
        from xlsx_exports import stream_book
        from openpyxl.styles import Font
        streams=[]
        for foreign_style in (False,True):
            stream=io.BytesIO()
            with stream_book() as book:
                if foreign_style:
                    sheet=book.create_sheet('foreign');cell=openpyxl.cell.WriteOnlyCell(sheet,'foreign')
                    cell.font=Font(name='Courier New');sheet.append([cell])
                s=layout._Sheet(book,'检查',[12]*8,None)
                s.row(['标题']*8,'header')
                for _ in range(20):
                    s.row(['文本',12,layout.NA,None,'固定折行','固定折行','多行\n文本',.25],formats={2:layout.MONEY,3:layout.INTEGER,8:layout.PERCENT})
                    s.row(['文本',12,layout.NA,None,'固定折行','固定折行','单行文本',.25],kind='even')
                book.save(stream)
            streams.append(stream.getvalue())
        for raw in streams:
            book=openpyxl.load_workbook(io.BytesIO(raw));s=book['检查']
            try:
                self.assertEqual(s['A2'].font.name,'Microsoft YaHei')
                self.assertEqual(s['A1'].alignment.horizontal,'center');self.assertTrue(s['A1'].alignment.wrap_text)
                self.assertEqual(s['B2'].number_format,layout.MONEY);self.assertEqual(s['B3'].number_format,'General')
                self.assertEqual(s['C2'].alignment.horizontal,'right');self.assertIsNone(s['C3'].alignment.horizontal)
                self.assertTrue(s['G2'].alignment.wrap_text);self.assertFalse(s['G3'].alignment.wrap_text)
                self.assertEqual(s['H2'].number_format,layout.PERCENT);self.assertEqual(s['H3'].number_format,'General')
                self.assertEqual(s['A2'].fill.fgColor.rgb,'FFFFFFFF');self.assertEqual(s['A3'].fill.fgColor.rgb,'FFF3F6FA')
                self.assertEqual(s['A40'].font.name,'Microsoft YaHei');self.assertEqual(s['A40'].data_type,'s')
            finally:book.close()


if __name__=='__main__':unittest.main()
