"""Privacy, bounded retention and failure isolation of lightweight diagnostics."""
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

import performance_log as perf
from export_progress import RowProgress
from runtime_support import Cancelled,CommitProgress


class FakeCounters:
    def __init__(self):self.n=0
    def process(self,pid=None,handle=None):
        self.n+=1
        return {'pid':os.getpid() if pid is None else pid,'identity':1,'cpu_seconds':self.n/100,
                'private_bytes':100,'working_bytes':200,'read_bytes':self.n*10,'write_bytes':self.n*20}
    def system(self):return {'logical_cpus':4,'physical_available_bytes':1000,'cpu_total_ticks':self.n*100,'cpu_idle_ticks':self.n*20}


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.prior=perf._current
        self.monitor=perf.Monitor(self.root/'logs',interval=.02,counters=FakeCounters())
        perf._current=self.monitor
    def tearDown(self):
        self.monitor.stop();perf._current=self.prior;self.temp.cleanup()
    def records(self):
        return [json.loads(line) for p in perf.log_files(self.monitor.folder) for line in p.read_text('utf-8').splitlines()]

    def test_progress_records_fixed_phases_never_business_content(self):
        with perf.begin('return_forms.save') as span:
            span.progress(RowProgress('门店敏感名 商品5009034892013 E:\私人\账单.xlsx 编码内容块：9/10',9,10))
            span.progress(CommitProgress(True));span.progress(CommitProgress(False))
        records=[]
        while not self.monitor.events.empty():records.append(self.monitor.events.get_nowait())
        encoded=json.dumps(records,ensure_ascii=False)
        self.assertNotIn('敏感名',encoded);self.assertNotIn('5009034892013',encoded);self.assertNotIn('账单',encoded)
        end=records[-1];self.assertEqual(end['current'],9);self.assertEqual(end['total'],10)
        self.assertEqual(set(end['phases']),{'prepare','encoding','commit','post_commit'})
        self.assertEqual(end['outcome'],'ok')

    def test_original_exceptions_and_cancellation_are_not_swallowed(self):
        marker=RuntimeError('sensitive original message')
        with self.assertRaises(RuntimeError) as ctx:
            with perf.begin('orders.save'):raise marker
        self.assertIs(ctx.exception,marker)
        with self.assertRaises(Cancelled):
            with perf.begin('orders.save'):raise Cancelled('业务原因')
        ended=[self.monitor.events.get_nowait() for _ in range(self.monitor.events.qsize())]
        ends=[r for r in ended if r['kind']=='operation_end']
        self.assertEqual([r['outcome'] for r in ends],['failed','cancelled'])
        self.assertNotIn('sensitive',json.dumps(ended));self.assertNotIn('业务原因',json.dumps(ended,ensure_ascii=False))

    def test_queue_and_concurrent_active_tasks_are_bounded(self):
        spans=[perf.begin('orders.save') for _ in range(10000)]
        self.assertEqual(len(self.monitor.active),perf.MAX_ACTIVE)
        for _ in range(10000):self.monitor.emit('phase')
        self.assertEqual(self.monitor.events.qsize(),perf.MAX_QUEUE);self.assertGreater(self.monitor.dropped,0)
        for span in spans:span.__exit__(None,None,None)
        self.assertFalse(self.monitor.active)

    def test_short_operation_is_kept_between_resource_samples(self):
        self.monitor.start()
        with perf.begin('orders.save'):pass
        self.monitor.stop()
        records=self.records()
        self.assertTrue(any(r['kind']=='operation_end' for r in records));self.assertEqual(records[-1]['kind'],'stopped')
        self.assertFalse(self.monitor.thread.is_alive())

    def test_stop_racing_a_write_drains_the_last_operation(self):
        entered=threading.Event();release=threading.Event();original=self.monitor.write
        def delayed(records):
            if not entered.is_set():entered.set();release.wait(1)
            return original(records)
        with patch.object(self.monitor,'write',side_effect=delayed):
            self.monitor.start();self.assertTrue(entered.wait(1))
            with perf.begin('orders.save'):pass
            self.monitor.stopped.set();release.set();self.monitor.stop()
        records=self.records();self.assertTrue(any(r['kind']=='operation_end' for r in records));self.assertEqual(records[-1]['kind'],'stopped')

    def test_retained_exited_processes_do_not_fill_registration_slots(self):
        class Process:
            def __init__(self):self.running=True
            def poll(self):return None if self.running else 0
        held=[]
        for _ in range(100):
            p=Process();self.monitor.owned(p,'aoxiang-component');held.append(p);p.running=False
        latest=Process();self.monitor.owned(latest,'aoxiang-component')
        self.assertEqual(len(self.monitor.processes),1);self.assertIs(self.monitor.processes[0][0](),latest)

    def test_background_sampler_has_resources_and_no_busy_loop(self):
        self.monitor.start();time.sleep(.11);self.monitor.stop()
        samples=[r for r in self.records() if r['kind']=='sample']
        self.assertGreaterEqual(len(samples),2);self.assertLessEqual(len(samples),8)
        self.assertTrue(any('cpu_cores' in r['processes'][0] for r in samples[1:]))
        self.assertTrue(all(r['processes'][0]['role']=='main' for r in samples))

    def test_explicit_export_flushes_a_just_finished_operation(self):
        self.monitor.interval=5;self.monitor.start()
        with perf.begin('orders.save'):pass
        target=self.root/'recent.zip';perf.export(self.monitor.folder,target)
        with zipfile.ZipFile(target) as z:
            summary=json.loads(z.read('摘要.json'));records=[json.loads(v) for v in z.read('性能日志.jsonl').splitlines()]
            self.assertTrue(summary['recent_flush_succeeded'])
            self.assertTrue(any(v['kind']=='operation_end' and v['operation']=='orders.save' for v in records))

    def test_failed_counter_and_writer_do_not_affect_operation(self):
        with patch.object(self.monitor,'sample',side_effect=OSError('private path')),patch.object(self.monitor,'write',side_effect=OSError('disk full')):
            self.monitor.start()
            with perf.begin('orders.save') as span:span.progress('保存业务')
            time.sleep(.07);self.monitor.stop()
        self.assertGreater(self.monitor.sample_errors,0);self.assertGreater(self.monitor.write_errors,0)
        self.assertFalse(self.monitor.active)

    def test_write_occurs_only_in_background(self):
        threads=[];original=self.monitor.write
        def write(records):threads.append(threading.current_thread().name);return original(records)
        with patch.object(self.monitor,'write',side_effect=write):
            self.monitor.start()
            for _ in range(100):
                with perf.begin('orders.save'):pass
            time.sleep(.04);self.monitor.stop()
        self.assertTrue(threads);self.assertTrue(all(n=='light-performance-log' for n in threads))

    def test_ttl_prunes_only_owned_regular_logs(self):
        folder=self.monitor.folder;folder.mkdir();now=time.time()
        old=folder/'perf-v1-1-123-aaaaaaaaaaaa-1.jsonl';old.write_text('old');os.utime(old,(now-perf.RETENTION-1,)*2)
        keep=folder/'app.log';keep.write_text('other diagnostics');os.utime(keep,(now-perf.RETENTION-1,)*2)
        recent=folder/'perf-v1-2-123-aaaaaaaaaaaa-2.jsonl';recent.write_text('recent')
        self.monitor.prune(now)
        self.assertFalse(old.exists());self.assertTrue(keep.exists());self.assertTrue(recent.exists())

    def test_rolling_files_stay_under_cap(self):
        with patch.object(perf,'FILE_BYTES',512),patch.object(perf,'MAX_BYTES',2048):
            for i in range(100):self.monitor.write([{'at':time.time(),'kind':'sample','text':'a'*260}])
            self.assertLessEqual(sum(p.stat().st_size for p in perf.log_files(self.monitor.folder)),2048)

    def test_export_strictly_filters_24h_and_unknown_fields(self):
        now=time.time();session='b'*12
        records=[{'at':now-perf.RETENTION-10,'session':session,'kind':'operation_end','id':'1','operation':'orders.save','seconds':9,'phases':{}},
                 {'at':now-1,'session':session,'kind':'operation_end','id':'2','operation':'orders.save','seconds':3,'outcome':'ok',
                  'phases':{'encoding':2,'sensitive business field':99},'raw_file':'D:/private.xlsx','sku':'sensitive'},
                 {'at':now-1,'session':session,'kind':'sample','processes':[{'pid':42,'private_bytes':99,'working_bytes':101,'command':'secret','role':'main'}],
                  'system':{'physical_available_bytes':500},'active':[{'operation':'orders.save','phase':'encoding','text':'secret'}],'gui_lag_seconds':1.5}]
        self.monitor.write(records)
        target=self.root/'performance.zip';self.assertEqual(perf.export(self.monitor.folder,target),str(target))
        with zipfile.ZipFile(target) as z:
            self.assertEqual(set(z.namelist()),{'说明.txt','环境.json','性能日志.jsonl','摘要.json'})
            raw=z.read('性能日志.jsonl').decode();summary=json.loads(z.read('摘要.json'))
            self.assertNotIn('secret',raw);self.assertNotIn('private.xlsx',raw);self.assertNotIn('sensitive',raw)
            self.assertEqual(summary['records'],2);self.assertEqual(summary['completed_operations'],1)
            self.assertEqual(summary['invalid_or_outside_24h'],1);self.assertEqual(summary['phase_seconds'],{'encoding':2})
            self.assertEqual(summary['peak_tree_private_bytes'],99);self.assertEqual(summary['maximum_gui_lag_seconds'],1.5)

    def test_cancel_export_removes_only_private_temporary_zip(self):
        self.monitor.write([{'at':time.time(),'kind':'stopped','session':'c'*12}]);event=threading.Event();event.set()
        with self.assertRaises(Cancelled):perf.export(self.monitor.folder,self.root/'cancel.zip',cancel=event)
        self.assertFalse((self.root/'cancel.zip').exists());self.assertFalse(list(self.root.glob('.performance-*.zip')))
        self.assertTrue(perf.log_files(self.monitor.folder))

    def test_export_does_not_overwrite_existing_file(self):
        target=self.root/'existing.zip';target.write_bytes(b'keep')
        with self.assertRaises(FileExistsError):perf.export(self.monitor.folder,target)
        self.assertEqual(target.read_bytes(),b'keep')

    def test_empty_logs_export_explains_missing_samples(self):
        target=self.root/'empty.zip';perf.export(self.root/'absent',target)
        with zipfile.ZipFile(target) as z:
            self.assertEqual(json.loads(z.read('摘要.json'))['samples'],0);self.assertEqual(z.read('性能日志.jsonl'),b'')

    def test_work_io_wrapper_preserves_return_identity_and_progress(self):
        marker=object();messages=[]
        class Session:
            @perf.work_io
            def call(self,operation,value,progress):progress(RowProgress('编码：1/2',1,2));return value
        self.assertIs(Session().call('save',marker,messages.append),marker)
        self.assertEqual(messages[0].current,1)

    def test_component_wrapper_does_not_log_arguments_or_results(self):
        marker=object()
        class Client:
            @perf.component
            def request(self,action,*,timeout=3,**values):return values['value']
        self.assertIs(Client().request('upload',value=marker,password='secret'),marker)
        self.assertNotIn('secret',str(list(self.monitor.events.queue)))

    def test_heartbeat_lag_is_measured_without_qt_access_in_writer(self):
        self.monitor.heartbeat_at=time.monotonic()-3
        self.monitor.heartbeat();self.assertGreaterEqual(self.monitor.gui_lag,1.9)
        record=self.monitor.sample();self.assertGreaterEqual(record['gui_lag_seconds'],1.9)

    def test_missing_counters_are_not_claimed_as_zero(self):
        self.monitor.counters=type('Missing',(),{'process':lambda *a:{'pid':os.getpid()},'system':lambda *a:{}})()
        self.monitor.sample();record=self.monitor.sample()
        self.assertNotIn('private_bytes',record['processes'][0]);self.assertNotIn('cpu_cores',record['processes'][0])

    def test_native_counters_query_self_without_process_enumeration(self):
        from performance_counters import Counters
        counters=Counters();record=counters.process();system=counters.system()
        self.assertEqual(record['pid'],os.getpid());self.assertIn('cpu_seconds',record)
        if os.name=='nt':
            for key in ('identity','private_bytes','working_bytes','read_bytes','write_bytes'):self.assertGreater(record[key],0)
            self.assertGreater(system['physical_available_bytes'],0)

    def test_cancelled_shutdown_does_not_disable_recording(self):
        from application_runtime import Runtime
        from unittest.mock import Mock
        runtime=Runtime.__new__(Runtime);runtime.emit=Mock();runtime.performance=Mock()
        for phase in ('WAITING_EXIT','SHUTTING_DOWN','RUNNING'):runtime.transition(phase)
        runtime.performance.stop.assert_not_called()
        runtime.transition('EVENT_LOOP_EXIT');runtime.performance.stop.assert_called_once()
        runtime.performance.stop.side_effect=OSError('logging failed')
        runtime.transition('UI_TEARDOWN')
        self.assertEqual(runtime.phase,'UI_TEARDOWN')


if __name__=='__main__':unittest.main()
