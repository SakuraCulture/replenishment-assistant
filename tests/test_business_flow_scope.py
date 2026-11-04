"""Synthetic combination and cold-index cancellation tests, independent of Qt."""
import hashlib
import itertools
from pathlib import Path
import sys
import tempfile
import threading
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tools')]
from business_flow_fixture import prepare
import business_flow
import daily_batch
from runtime_support import Cancelled


class ScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();cls.root=Path(cls.tmp.name)/'fixture'
        prepare(cls.root,60,2);cls.path=cls.root/'settings/workbench.sqlite3'
        cls.cache=cls.root/'scope.sqlite3'

    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()

    def test_date_warehouse_lock_search_stage_combinations_exact_order(self):
        full=daily_batch.read(self.path,self.cache,{'all_matched':True})['records']
        key=lambda r:(r['oid'],r['warehouse'])
        for day,wh,lock,term,stage in itertools.product(('', '2026-11-04'),('', '河北仓','山东仓'),('', '已锁','未锁'),('', 'S001'),('', 'return','payment','receipt')):
            request={'from':day,'to':day,'warehouse':wh,'lock':lock,'search':term,'stage_view':stage,'stage_filter':'待处理','all_matched':True}
            expected=[r for r in full if (not day or r['day']==day) and (not wh or r['warehouse']==wh)
                      and (not lock or r['locked']==(lock=='已锁')) and (not term or r['store']==term)
                      and (not stage or r['next']==stage)]
            with self.subTest(request=request):
                actual=daily_batch.read(self.path,self.cache,request)['records']
                self.assertEqual(list(map(key,actual)),list(map(key,expected)))

    def test_paging_scope_and_source_immutability(self):
        before=hashlib.sha256(self.path.read_bytes()).hexdigest()
        a=daily_batch.read(self.path,self.cache,{'page':0});b=daily_batch.read(self.path,self.cache,{'page':1})
        self.assertEqual((len(a['records']),len(b['records'])),(50,50))
        keys=[[b['records'][0]['oid'],b['records'][0]['warehouse']],[a['records'][0]['oid'],a['records'][0]['warehouse']]]
        rows=daily_batch.read(self.path,self.cache,{'all_matched':True,'scope':keys})['records']
        self.assertEqual([[r['oid'],r['warehouse']] for r in rows],list(reversed(keys)))
        self.assertEqual(daily_batch.read(self.path,self.cache,{'scope':[]})['records'],[])
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(),before)

    def test_cancel_cold_index_then_retry_does_not_change_business_database(self):
        before=hashlib.sha256(self.path.read_bytes()).hexdigest();event=threading.Event()
        def progress(p):
            if p['done']>=2:event.set()
        with self.assertRaises(Cancelled):business_flow.read_home(self.path,self.root/'cancel-index.sqlite3',event,progress)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(),before)
        event.clear();a=business_flow.read_home(self.path,self.root/'cancel-index.sqlite3',event)
        b=business_flow.read_home(self.path,self.cache)
        self.assertEqual(a,b);self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(),before)


if __name__=='__main__':unittest.main()
