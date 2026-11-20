"""Scratch adaptation on isolated synthetic orders; no platform or user data."""
import snapshot_json
import copy
import hashlib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import openpyxl
import adaptive_resources as resources
import encoding_store
import normal_exports
import order_identity
import storage_chunks
import work_repository as wr
from runtime_support import Cancelled
from test_archive_orders import seed

MIB = resources.MIB


class Source:
    def __init__(self):
        self.now=0
        self.values={'physical_total_bytes':32*1024*MIB,'physical_available_bytes':8*1024*MIB,
                     'commit_limit_bytes':64*1024*MIB,'commit_total_bytes':20*1024*MIB,
                     'private_bytes':512*MIB}
    def __call__(self):return dict(self.values)
    def clock(self):return self.now
    def tight(self):self.values['physical_available_bytes']=512*MIB;self.now+=1
    def ample(self):self.values['physical_available_bytes']=8*1024*MIB;self.now+=1
    def budget(self):return resources.Budget(self,self.clock)


class Budgets(unittest.TestCase):
    def test_uses_system_commit_headroom_not_only_free_physical_memory(self):
        source=Source();source.values['commit_total_bytes']=source.values['commit_limit_bytes']-128*MIB
        budget=source.budget();self.assertEqual(budget.mode,'critical');self.assertEqual(budget.cache_bytes,0)
        self.assertFalse(budget.export_fits(10))

    def test_process_limit_is_respected_even_on_large_machine(self):
        source=Source();source.values['process_limit_bytes']=source.values['private_bytes']+128*MIB
        budget=source.budget();self.assertEqual(budget.extra_bytes,16*MIB)
        self.assertLess(budget.fast_json_bytes,8*MIB)

    def test_shrink_immediately_and_recover_only_after_stable_delay(self):
        source=Source();budget=source.budget();self.assertEqual(budget.extra_bytes,256*MIB)
        source.tight();budget.refresh();self.assertEqual(budget.extra_bytes,0)
        source.ample();budget.refresh();self.assertEqual(budget.extra_bytes,0)
        source.now+=2;budget.refresh();self.assertEqual(budget.extra_bytes,0)
        source.now+=1;budget.refresh();self.assertEqual(budget.extra_bytes,16*MIB)
        self.assertEqual((budget.shrinks,budget.grows),(1,1))

    def test_missing_counters_and_counter_failure_are_conservative(self):
        for provider in (lambda:{},lambda:{'physical_available_bytes':8*1024*MIB},lambda:(_ for _ in ()).throw(OSError('probe failed'))):
            budget=resources.Budget(provider);self.assertEqual(budget.mode,'unknown')
            self.assertEqual(budget.fast_json_bytes,resources.STREAM_BUFFER)
            self.assertFalse(budget.export_fits(1))

    def test_restore_budget_includes_whole_json_unicode_and_decoded_rows(self):
        source=Source();budget=source.budget()
        self.assertTrue(budget.restore_fits(8*MIB));self.assertFalse(budget.restore_fits(64*MIB))
        source.tight();self.assertFalse(budget.restore_fits(1))

    def test_pressure_mid_encode_trims_cache_and_preserves_canonical_bytes(self):
        source=Source();budget=source.budget()
        with tempfile.TemporaryDirectory() as root,encoding_store.Store(Path(root)/'db',resources=budget) as store:
            value={'rows':[{'sku':str(n),'qty':n,'note':'测试'*19} for n in range(1200)]}
            prepared=storage_chunks.prepare(value,store=store)
            for key in prepared.chunks:
                list(store.read(key))
            self.assertGreater(store.cached,0)
            source.tight()
            for key in prepared.chunks:
                list(store.read(key))
            self.assertEqual(store.cached,0)
            self.assertEqual(prepared.digest,hashlib.sha256(wr.dumps(value).encode()).hexdigest())
            self.assertGreater(store.stats()['resource_budget']['shrinks'],0)

    def test_fast_encoder_allocation_failure_falls_back_before_emitting_container(self):
        source=Source();value=[{'sku':str(n),'qty':n,'text':'汉字'*50} for n in range(800)]
        expected=wr.dumps(value).encode();original=wr.dumps;state={'raised':False}
        def fail_once(obj):
            if obj is value and not state['raised']:
                state['raised']=True;raise MemoryError('synthetic allocation pressure')
            return original(obj)
        with tempfile.TemporaryDirectory() as root,encoding_store.Store(Path(root)/'db',resources=source.budget()) as store:
            parts=[]
            with patch.object(snapshot_json,'dumps',side_effect=fail_once):key,size=store.encode(value,parts.append)
            self.assertTrue(state['raised']);self.assertEqual(b''.join(parts),expected)
            self.assertEqual((key,size),(hashlib.sha256(expected).hexdigest(),len(expected)))
            self.assertEqual(store.pressure_fallbacks,1)


class Restore(unittest.TestCase):
    def test_formal_signed_export_uses_bounded_full_snapshot_and_keeps_payload(self):
        import warehouse_execution as execution,normal_delivery
        from contextlib import closing
        source=Source();source.tight()
        with tempfile.TemporaryDirectory() as root:
            work,repo,_=seed(Path(root));order=repo.get(wr.today(),'T')
            with closing(work.connect()) as db,db:execution.ensure_parent(db,order['oid'])
            with patch.object(resources,'snapshot',source),patch.object(storage_chunks,'BytesIO',side_effect=AssertionError('whole JSON buffer forbidden')):
                paths=normal_delivery.export_signed(repo,order,Path(root)/'formal.xlsx',['河北仓'])
            self.assertEqual(len(paths),1);self.assertTrue(Path(paths[0]).is_file())
            book=openpyxl.load_workbook(paths[0]);self.assertTrue(order_identity.read(book));book.close()

    def test_legacy_full_export_read_keeps_full_payload_and_business_fingerprint(self):
        import warehouse_execution as execution
        source=Source();source.tight()
        with tempfile.TemporaryDirectory() as root:
            work,repo,_=seed(Path(root));order=repo.get(wr.today(),'T');head=repo.head(wr.today(),'T')
            with patch.object(resources,'snapshot',source),patch.object(storage_chunks,'BytesIO',side_effect=AssertionError('whole JSON buffer forbidden')):
                restored=execution.get_order(repo,head,export_io=True)
            self.assertEqual(restored,order)
            self.assertEqual(__import__('normal_orders').fingerprint_owned(restored['payload']['result'],restored['payload'].get('new_focus')),head['digest'])

    def test_bounded_restore_preserves_all_values_order_dates_and_full_digest(self):
        import sqlite3
        from datetime import date,datetime
        value={'rows':[{'sku':str(n),'quantity':n+.25,'note':'汉字'*40,'date':date(2026,11,16),'stamp':datetime(2026,11,16,12,34)} for n in range(900)],
               'map':{str(n):[n,False,None,'=文字'] for n in range(900)}}
        with sqlite3.connect(':memory:') as db:
            packed=storage_chunks.prepare(value);blob=storage_chunks.put(db,packed)
            expected=storage_chunks.load(blob,db)
            with patch.object(storage_chunks,'BytesIO',side_effect=AssertionError('whole JSON buffer forbidden')):
                actual=storage_chunks.load(blob,db,bounded=True)
            self.assertEqual(actual,expected);self.assertEqual(wr.dumps(actual),wr.dumps(value))

    def test_bounded_restore_keeps_block_and_full_snapshot_corruption_checks(self):
        import sqlite3
        with sqlite3.connect(':memory:') as db:
            packed=storage_chunks.prepare({'rows':[{'sku':str(n),'qty':n} for n in range(900)]})
            blob=storage_chunks.put(db,packed)
            packed.digest='0'*64
            with self.assertRaisesRegex(ValueError,'完整快照校验失败'):storage_chunks.load(packed.blob(),db,bounded=True)
            db.execute('UPDATE snapshot_chunks SET size=size+1')
            with self.assertRaisesRegex(ValueError,'快照内容块校验失败'):storage_chunks.load(blob,db,bounded=True)


def workbook_facts(path):
    book=openpyxl.load_workbook(path,data_only=False)
    try:
        sheets=[]
        for ws in book:
            cells=[[(c.value,c.data_type,c.number_format,str(c.font),c.comment.text if c.comment else None) for c in row] for row in ws]
            sheets.append((ws.title,ws.sheet_state,cells,str(ws.sheet_format),str(ws.print_options),str(ws.oddHeader),
                           {k:(v.width,v.hidden) for k,v in ws.column_dimensions.items()}))
        return (sheets,[(p.name,p.value) for p in book.custom_doc_props],order_identity.read(book),str(book.security))
    finally:book.close()


class Exports(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.work,self.repo,self.payload=seed(self.root)
        self.order=self.repo.get(wr.today(),'T')
        base=self.order['payload']['result']['rows'][0]
        rows=[]
        for n in range(1100):
            row=copy.deepcopy(base);row.update(E=f'{n:013d}',F='0000000000001',D='=保留文字'+str(n))
            if n%17==0:row.update(_budget_active=True,_allocated_qty=0,_allocated_amount=0)
            rows.append(row)
        self.order['payload']['result']['rows']=rows
    def tearDown(self):self.temp.cleanup()

    def test_ample_and_tight_exports_keep_cells_styles_comments_and_identity(self):
        source=Source();normal=self.root/'normal.xlsx';stream=self.root/'stream.xlsx';before=copy.deepcopy(self.order)
        normal_exports.export(self.order,normal,warehouse='河北仓',resources=source.budget())
        source.tight();normal_exports.export(self.order,stream,warehouse='河北仓',resources=source.budget())
        self.assertEqual(workbook_facts(normal),workbook_facts(stream));self.assertEqual(self.order,before)

    def test_tight_export_releases_closed_workbook_cycles_before_next_archive(self):
        import weakref
        source=Source();source.tight();books=[];factory=openpyxl.Workbook
        def track(*args,**kwargs):
            book=factory(*args,**kwargs);books.append(weakref.ref(book));return book
        with patch.object(openpyxl,'Workbook',side_effect=track):
            for n in range(2):
                normal_exports.export(self.order,self.root/f'tight-{n}.xlsx',warehouse='河北仓',resources=source.budget())
                self.assertTrue(books);self.assertTrue(all(book() is None for book in books))

    def test_mid_export_pressure_rebuilds_only_unpublished_file_in_row_order(self):
        source=Source();target=self.root/'dynamic.xlsx';baseline=self.root/'baseline.xlsx';messages=[];changed=False
        normal_exports.export(self.order,baseline,warehouse='河北仓',resources=source.budget())
        def progress(message):
            nonlocal changed
            messages.append(str(message))
            if getattr(message,'current',None)==500 and not changed:source.tight();changed=True
        normal_exports.export(self.order,target,warehouse='河北仓',progress=progress,resources=source.budget())
        self.assertTrue(changed);self.assertTrue(any('改为逐行写入' in s for s in messages))
        self.assertEqual(workbook_facts(target),workbook_facts(baseline))
        self.assertFalse(list(self.root.glob('.normal-*')))

    def test_memoryerror_before_publication_switches_to_stream_once(self):
        source=Source();target=self.root/'memory.xlsx';original=normal_exports._write_workbook;calls=[]
        def fail_normal(*args):
            streamed=args[-1];calls.append(streamed)
            if not streamed:raise MemoryError('synthetic workbook pressure')
            return original(*args)
        with patch.object(normal_exports,'_write_workbook',side_effect=fail_normal):
            normal_exports.export(self.order,target,warehouse='河北仓',resources=source.budget())
        self.assertEqual(calls,[False,True]);self.assertTrue(workbook_facts(target)[2])

    def test_cancel_keeps_previous_output_and_cleans_owned_temp(self):
        target=self.root/'existing.xlsx';target.write_bytes(b'previous published output');cancel=threading.Event();source=Source();source.tight()
        def progress(message):
            if getattr(message,'current',None)==500:cancel.set()
        with self.assertRaises(Cancelled):normal_exports.export(self.order,target,warehouse='河北仓',cancel=cancel,progress=progress,resources=source.budget())
        self.assertEqual(target.read_bytes(),b'previous published output');self.assertFalse(list(self.root.glob('.normal-*')))

    def test_failed_publication_does_not_replay_or_replace_previous_output(self):
        target=self.root/'existing.xlsx';target.write_bytes(b'previous output');source=Source();source.tight()
        with patch.object(normal_exports.os,'replace',side_effect=PermissionError('locked')) as replace:
            with self.assertRaisesRegex(PermissionError,'已有文件未替换'):
                normal_exports.export(self.order,target,warehouse='河北仓',resources=source.budget())
            self.assertEqual(replace.call_count,1)
        self.assertEqual(target.read_bytes(),b'previous output')


if __name__=='__main__':unittest.main()
