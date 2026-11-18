"""Frozen fingerprint bytes, disk-spooled validation, and nonempty failures."""
import copy,hashlib,json,math,os,tempfile,threading,unittest
from datetime import date,datetime,timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
import analytics_snapshot as streaming
import order_analytics as analytics
from runtime_support import Cancelled,CommitProgress
from export_progress import RowProgress


def old_digest(values):return hashlib.sha256(json.dumps(values,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False,default=str).encode('utf-8')).hexdigest()


class SnapshotBytes(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.folder=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def test_exact_legacy_compact_json_edge_cases(self):
        for value in (None,False,0,-0.0,1.0,1e-8,2**120,'中文\n"\\😀',Decimal('1.2000'),date(2026,11,17),
                      datetime(2026,11,17,tzinfo=timezone.utc),{2:'two',10:'ten'},[None,Decimal('-0.00')]):
            refs={'signed_number':'BH-0001','purchases':[]};rows=[{'z':value,'references':refs,'a':'00001','quantities':{'x':value}}]
            parts=['funnel-4',{'search':'测试'},[[value]],rows,[value],[{'references':refs}]]
            self.assertEqual(streaming.snapshot_digest(*parts),old_digest(parts))
            with streaming.FactSpool(self.folder) as spool:
                for row in rows:spool.append(row)
                self.assertEqual(streaming.snapshot_digest(*parts[:3],spool,*parts[4:]),old_digest(parts))

    def test_references_attached_after_fact_write_are_not_stale(self):
        first={'purchases':[],'signed_number':'A'};second={'purchases':[],'signed_number':'B'}
        rows=[{'sku':str(i),'references':first if i<5 else second,'amount':Decimal('1.30')} for i in range(10)]
        with streaming.FactSpool(self.folder) as spool:
            for row in rows:spool.append(row)
            first['purchases'].append({'number':'P1','state':'BOUND'});second['purchases'].append({'number':'P2','status':'已作废'})
            self.assertEqual(len(spool.references),2)
            self.assertEqual(streaming.snapshot_digest('v',{},[],spool,[],[first,second]),old_digest(['v',{},[],rows,[],[first,second]]))

    def test_no_references_and_null_references_have_exact_original_bytes(self):
        rows=[{}, {'references':None}, {'references':{},'a':1}, {'z':2,'references':{},'a':1}]
        with streaming.FactSpool(self.folder) as spool:
            for row in rows:spool.append(row)
            self.assertEqual(streaming.snapshot_digest('v',{},[],spool,[],[]),old_digest(['v',{},[],rows,[],[]]))

    def test_empty_rows_and_repeated_reads_keep_hash(self):
        with streaming.FactSpool(self.folder) as spool:
            expected=old_digest(['v',{},[],[],[],[]])
            self.assertEqual(streaming.snapshot_digest('v',{},[],spool,[],[]),expected)
            self.assertEqual(streaming.snapshot_digest('v',{},[],spool,[],[]),expected)

    def test_nonfinite_and_cycles_still_rejected(self):
        for invalid in (float('nan'),float('inf'),Decimal('NaN')):
            parts=['v',{},[],[{'amount':invalid}],[],[]]
            if isinstance(invalid,Decimal):self.assertEqual(streaming.snapshot_digest(*parts),old_digest(parts))
            else:
                with self.assertRaises(ValueError):streaming.snapshot_digest(*parts)
        cycle=[];cycle.append(cycle)
        with self.assertRaises(ValueError):streaming.snapshot_digest('v',{},[],[{'cycle':cycle}],[],[])

    def test_cancel_and_error_close_delete_temporary_file(self):
        stop=threading.Event()
        with self.assertRaises(Cancelled):
            with streaming.FactSpool(self.folder,stop) as spool:
                spool.append({'a':1});stop.set();spool.update_hash(hashlib.sha256())
        self.assertTrue(spool.stream.closed);self.assertFalse(list(self.folder.glob('.analytics-verify-*')))
        with self.assertRaises(OSError):
            with streaming.FactSpool(self.folder) as spool:
                with patch.object(spool,'append',side_effect=OSError('disk full')):spool.append({})
        self.assertTrue(spool.stream.closed);self.assertFalse(list(self.folder.glob('.analytics-verify-*')))

    def test_truncated_frame_fails_instead_of_hashing_partial_content(self):
        with streaming.FactSpool(self.folder) as spool:
            spool.append({'a':1});spool.stream.truncate(1);spool.stream.seek(1)
            with self.assertRaisesRegex(ValueError,'不完整'):spool.update_hash(hashlib.sha256())

    def test_fact_payload_is_not_retained_by_sink(self):
        refs={'purchases':[]}
        with streaming.FactSpool(self.folder) as spool:
            for i in range(10000):spool.append({'name':'x'*1000,'sku':str(i),'references':refs})
            self.assertEqual(spool.count,10000);self.assertEqual(spool.references,[refs])
            self.assertEqual(set(vars(spool)),{'stream','cancel','progress','references','count'})


class ProjectionValidation(unittest.TestCase):
    def setUp(self):
        from test_order_analytics import AnalyticsTests
        self.fixture=AnalyticsTests();self.fixture.setUp();self.path=self.fixture.path
    def tearDown(self):self.fixture.tearDown()

    def test_same_snapshot_uses_no_second_derived_report(self):
        report=self.fixture.report()
        with patch('order_analytics.aggregate',side_effect=AssertionError('second aggregate')),patch('history_diagnostics.review',side_effect=AssertionError('second history')):
            self.assertTrue(analytics.validate_snapshot(self.path,report))

    def test_normal_query_keeps_old_snapshot_hash_and_rows(self):
        original=streaming.snapshot_digest;captured=[]
        def compare(*args,**kwargs):
            actual=original(*args,**kwargs);self.assertEqual(actual,old_digest(list(args[:6])));captured.append(actual);return actual
        with patch('analytics_snapshot.snapshot_digest',side_effect=compare):report=self.fixture.report()
        self.assertEqual(captured,[report['snapshot_id']]);self.assertTrue(report['rows'])

    def test_all_filters_keep_same_full_validation_contract(self):
        for filters in ({},{'group':'必采'},{'warehouse':'河北仓'},{'search':'not-present'},{'as_of':'2026-11-17T23:59:59+08:00'}):
            with self.subTest(filters=filters):
                report=self.fixture.report(**filters);self.assertTrue(analytics.validate_snapshot(self.path,report))


class FailureMessages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])

    def test_original_nonempty_business_errors_are_not_rewritten(self):
        from diagnostics import task_failure
        for message in ('付款资料待核','database is locked','失败阶段：x\n原因：y\n提交状态：已提交'):
            self.assertEqual(task_failure(ValueError(message),'reading'),message)

    def test_empty_exceptions_always_include_type_and_stage(self):
        from diagnostics import task_failure
        for exc in (MemoryError(),AssertionError(),OSError(),RuntimeError()):
            value=task_failure(exc,'读取签发与履约快照')
            self.assertIn(type(exc).__name__,value);self.assertIn('读取签发与履约快照',value)
        self.assertIn('内存',task_failure(MemoryError()))

    def test_memory_failure_with_text_keeps_its_cause(self):
        from diagnostics import task_failure
        self.assertIn('explicit cause',task_failure(MemoryError('explicit cause')))

    def test_error_presentation_itself_has_a_nonempty_fallback(self):
        from readable_ui import failure_message
        for value in (MemoryError(),AssertionError(),'',' '):self.assertTrue(failure_message(value).strip())

    def test_worker_reports_original_type_and_stage_even_if_logging_fails(self):
        from ui_tasks import Worker
        messages=[]
        def failed(*,progress,cancel):progress('读取签发与履约快照');raise MemoryError()
        worker=Worker(failed,(),None);worker.failed.connect(messages.append)
        with patch('ui_tasks.logging.exception',side_effect=OSError('log full')):worker.run()
        self.assertEqual(len(messages),1);self.assertIn('MemoryError',messages[0]);self.assertIn('读取签发与履约快照',messages[0])

    def test_cancellation_still_uses_original_signal(self):
        from ui_tasks import Worker
        cancelled=[];errors=[]
        def failed(*,progress,cancel):raise Cancelled('已取消')
        worker=Worker(failed,(),None);worker.failed.connect(errors.append);worker.cancelled.connect(lambda:cancelled.append(True));worker.run()
        self.assertEqual(cancelled,[True]);self.assertEqual(errors,[])


if __name__=='__main__':unittest.main()
