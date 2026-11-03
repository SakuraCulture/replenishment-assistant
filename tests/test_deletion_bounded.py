"""Portable regression for exact deletion semantics, cancellation and bounded edits."""
import copy,datetime,hashlib,json,random,re,sqlite3,sys,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import data_maintenance as m
import maintenance_storage as ms
import storage_chunks as c
import work_repository as wr
from runtime_support import Cancelled
import test_data_maintenance as legacy_tests
from contextlib import closing

class BlockEdits(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.scratch=sqlite3.connect(':memory:')
        self.addCleanup(self.db.close);self.addCleanup(self.scratch.close)
    def payload(self):
        rows=[({'B':'T' if i%3==0 else 'U','E':str(i)}, {'source_row':i,'none':None,'zero':0,'blank':''}) for i in range(900)]
        import engine
        field=engine.config()['sales_fields']['store_code']
        return {'schema':1,'day':wr.today(),'model_version':'0.3.4-rules','input_dirty':False,
            'states':{s:{'name':s,'state':'已完成'} for s in ['T','U']},
            'results':{s:{'summary':{'amount':12},'rows':[{'E':str(i),'flag':False} for i in range(600)]} for s in ['T','U']},
            'catalogs':{'items':{str(i):{'tag':'医疗','n':i} for i in range(300)}},
            'loaded':{'stores':{'T':'T','U':'U'},'blocked':{'T':1,'U':0},'normalized':rows,'warehouse_rows':{'河北仓':rows,'山东仓':rows},
                      'intake':{'stores':{'T':'T','U':'U'},'conflicts':[{'store':'T'},{'store':'U'}],
                                'selected':[{'values':{field:'T'}},{'values':{field:'U'}}]}},
            'export_log':[{'items':[{'store':'T'}]},{'items':[{'store':'U'}]}],
            'pending_copy':{'x':1},'date':datetime.date(2026,11,3),**{k:{'T':0,'U':None} for k in ms.STORE_MAPS if k not in ('states','results')}}
    def compare(self,p,legacy=False):
        expected=m.strip_work(p,{'T'})
        blob=__import__('normal_orders').encode(p) if legacy else c.put(self.db,c.prepare(p))
        editor=ms.Editor(self.db,self.scratch);out=editor.rewrite(blob,{'T'},m.strip_work)
        c.schema(self.db)
        self.db.executemany('INSERT OR IGNORE INTO snapshot_chunks VALUES(?,?,?)',self.scratch.execute('SELECT hash,size,data FROM snapshot_chunks'))
        result=c.load(c.put(self.db,out),self.db,expected=out.digest)
        self.assertEqual(wr.dumps(result),wr.dumps(expected));self.assertEqual(out.digest,hashlib.sha256(wr.dumps(expected).encode()).hexdigest())
        return editor,out
    def test_chunked_types_order_dates_and_shared_fields(self):self.compare(self.payload())
    def test_legacy_equivalent(self):self.compare(self.payload(),True)
    def test_legacy_progress_is_bounded_per_block_not_per_row(self):
        import zlib
        messages=[];editor=ms.Editor(self.db,self.scratch,messages.append)
        tree=editor.legacy_tree(zlib.compress(wr.dumps(list(range(9000))).encode()))
        self.assertEqual(len(messages),1)
        self.assertEqual(editor.value(tree),list(range(9000)))
    def test_empty_and_missing_optional_fields(self):
        for p in ({'catalogs':{}},{'loaded':None},{'loaded':{'stores':{'T':'T'}}},{'loaded':{'intake':{'frozen':True}}}):
            with self.subTest(p=p):self.compare(p)
    def test_many_store_map_blocks(self):self.compare({'states':{str(i):i for i in range(800)},'catalogs':{'T':'keep'}})
    def test_editor_avoids_full_loaded_decode(self):
        p=self.payload();blob=c.put(self.db,c.prepare(p));editor=ms.Editor(self.db,self.scratch)
        original=editor.value
        def bounded(node):
            if node[0]=='dict':self.assertFalse({'normalized','stores','warehouse_rows'}.issubset({k for k,_ in node[1]}))
            return original(node)
        with patch.object(editor,'value',side_effect=bounded):editor.rewrite(blob,{'T'},m.strip_work)
    def test_cancellation_during_streamed_read(self):
        blob=c.put(self.db,c.prepare(self.payload()));event=threading.Event()
        editor=ms.Editor(self.db,self.scratch,lambda _:event.set(),event)
        with self.assertRaises(Cancelled):editor.rewrite(blob,{'T'},m.strip_work)
    def test_original_manifest_and_block_corruption_rejected(self):
        blob=c.put(self.db,c.prepare(self.payload()));editor=ms.Editor(self.db,self.scratch)
        self.db.execute('UPDATE snapshot_chunks SET size=size+1')
        with self.assertRaisesRegex(ValueError,'校验'):editor.rewrite(blob,{'T'},m.strip_work)
    def test_owned_filter_does_not_mutate_other_inputs(self):
        p=self.payload();before=wr.dumps(p);out=m.strip_work(p,{'T'},owned=True)
        self.assertEqual(wr.dumps(p),before);self.assertEqual(wr.dumps(out),wr.dumps(m.strip_work(p,{'T'})))

class ReferenceScan(unittest.TestCase):
    def test_legacy_stream_refs_and_last_duplicate_key(self):
        import zlib
        texts=[wr.dumps({'x':['a'*64,datetime.date(2026,11,3)],'work_revision':'rev'}),
               '{"x":"'+('b'*64)+'","x":"'+('c'*64)+'","work_revision":null}',
               '{"x":"\\u0061'+('a'*63)+'","number":1e100,"s":"中文"}']
        with closing(sqlite3.connect(':memory:')) as db:
            ms.cache_schema(db)
            for text in texts:
                blob=zlib.compress(text.encode());expected=ms.hashes(wr.dumps(wr.loads(text)))
                self.assertEqual(ms.legacy_info(db,blob)['refs'],expected)
                with patch.object(ms.streaming_json,'parse',side_effect=AssertionError('cache not reused')):
                    self.assertEqual(ms.legacy_info(db,blob)['refs'],expected)
            with self.assertRaises(Exception):ms.legacy_info(db,zlib.compress(b'{broken'))
    def test_legacy_tree_never_calls_full_load(self):
        import zlib
        with closing(sqlite3.connect(':memory:')) as db,closing(sqlite3.connect(':memory:')) as scratch:
            editor=ms.Editor(db,scratch);data={'states':{'T':1,'U':2},'rows':[{'n':i,'d':datetime.date(2026,11,3)} for i in range(800)]}
            raw=wr.dumps(data).encode();blob=zlib.compress(raw)
            with patch.object(c,'load',side_effect=AssertionError('full decode')):
                result=editor.rewrite(blob,{'T'},m.strip_work,hashlib.sha256(raw).hexdigest())
            self.assertEqual(wr.dumps(editor.value(result.tree)),wr.dumps(m.strip_work(data,{'T'})))
            with self.assertRaisesRegex(ValueError,'校验'):editor.legacy_tree(blob,'wrong')
            event=threading.Event();event.set()
            with self.assertRaises(Cancelled):ms.Editor(db,scratch,cancel=event).legacy_tree(blob)
    def test_exact_legacy_regex_including_utf8_and_window_edges(self):
        rng=random.Random(4);texts=['', 'A'+'f'*64+'Z', '1'*65, '中文'+('a'*64)+'®', 'x'*262140+'b'*64+'!', 'c'*600000,
            ''.join(rng.choice('ab0129gX中文\n') for _ in range(600000))]
        for text in texts:
            with self.subTest(size=len(text)):self.assertEqual(ms.hashes(text),sorted(set(re.findall(r'(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])',text))))
    def test_cached_refs_validate_content_and_checksum(self):
        with sqlite3.connect(':memory:') as db:
            import zlib
            ms.cache_schema(db);raw=wr.dumps({'sha':'a'*64}).encode();h=hashlib.sha256(raw).hexdigest();data=zlib.compress(raw)
            self.assertEqual(ms.references_for(db,h,len(raw),data),['a'*64])
            db.execute("UPDATE maintenance_chunk_refs SET refs='[]'")
            self.assertEqual(ms.references_for(db,h,len(raw),data),['a'*64])
            with self.assertRaises(ValueError):ms.references_for(db,h,len(raw),zlib.compress(b'corrupt'))
    def test_cancel_gc_rolls_back(self):
        with sqlite3.connect(':memory:') as db:
            db.executescript('CREATE TABLE normal_revisions(payload BLOB);CREATE TABLE normal_work_revisions(revision TEXT,payload BLOB);CREATE TABLE jobs(payload BLOB);')
            blob=c.put(db,c.prepare({'x':list(range(4096))}));db.execute('INSERT INTO jobs VALUES(?)',(blob,));event=threading.Event();count=[]
            original=ms.references_for
            def trigger(*args,**kw):
                count.append(1);result=original(*args,**kw);event.set();return result
            with patch.object(ms,'references_for',side_effect=trigger),self.assertRaises(Cancelled):m.collect_shared(db,Path('.'),event)
            self.assertEqual(len(count),1)

class DeletionRaces(unittest.TestCase):
    setUp=legacy_tests.DeleteOrders.setUp
    def test_surviving_job_exact(self):
        states={s:{'name':s,'state':'待计算'} for s in ('T','U')}
        payload={'schema':1,'day':wr.today(),'model_version':'0.3.4-rules','states':states,'input_dirty':True,'results':{},'store_ui':{'T':{'n':1},'U':{'n':2}}}
        self.w.save_job(wr.today(),payload,None)
        plan=m.prepare(self.path,self.heads);m.execute(self.path,plan)
        with closing(sqlite3.connect(self.path)) as db:
            summary,blob=db.execute('SELECT summary,payload FROM jobs').fetchone();actual=c.load(blob,db)
        expected=m.strip_work(payload,{'T'})
        self.assertEqual(wr.dumps(actual),wr.dumps(expected));self.assertEqual(wr.loads(summary),wr.summary(expected))
    def test_invalid_surviving_job_day_rejected(self):
        payload={'schema':1,'day':'2000-01-01','model_version':'0.3.4-rules','states':{'U':{'name':'U','state':'待计算'}}}
        with closing(sqlite3.connect(self.path)) as db,db:
            prepared=c.prepare(payload);db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(wr.today(),'r',prepared.digest,wr.now(),'{}',c.put(db,prepared)))
        plan=m.prepare(self.path,self.heads)
        with self.assertRaisesRegex(ValueError,'日期不一致'):m.execute(self.path,plan)
        self.assertEqual(len(self.r.headers()),1)
    def test_waiting_write_lock_can_cancel_then_retry(self):
        plan=m.prepare(self.path,self.heads);event=threading.Event();writer=sqlite3.connect(self.path)
        try:
            writer.execute('BEGIN IMMEDIATE')
            def progress(s):
                if '等待其他操作' in str(s):event.set()
            with self.assertRaises(Cancelled):m.execute(self.path,plan,progress=progress,cancel=event)
        finally:writer.rollback();writer.close()
        self.assertEqual(len(self.r.headers()),1);event.clear();m.execute(self.path,plan,cancel=event);self.assertEqual(self.r.headers(),[])
    def test_commit_boundary_finishes_despite_late_cancel(self):
        plan=m.prepare(self.path,self.heads);event=threading.Event()
        def progress(s):
            if getattr(s,'active',False):event.set()
        result=m.execute(self.path,plan,progress=progress,cancel=event)
        self.assertEqual(result['deleted'],1);self.assertEqual(self.r.headers(),[])
    def test_new_shared_work_added_after_prepare_rejected(self):
        plan=m.prepare(self.path,self.heads);original=m.prepare_rewrites
        def add(db,plan,scratch,progress,cancel):
            result=original(db,plan,scratch,progress,cancel)
            with closing(sqlite3.connect(self.path)) as other,other:other.execute('INSERT INTO normal_work_revisions VALUES(?,?,?)',('other',wr.today(),__import__('normal_orders').encode({})))
            return result
        with patch.object(m,'prepare_rewrites',side_effect=add),self.assertRaises(wr.Conflict):m.execute(self.path,plan)
        self.assertEqual(len(self.r.headers()),1)
    def test_cancel_before_lock_leaves_no_business_or_cache_writes(self):
        plan=m.prepare(self.path,self.heads);event=threading.Event()
        def progress(s):
            if '取得删除写锁' in str(s):event.set()
        with self.assertRaises(Cancelled):m.execute(self.path,plan,progress=progress,cancel=event)
        with closing(sqlite3.connect(self.path)) as db:self.assertNotIn('maintenance_chunk_refs',m.tables(db))
        self.assertEqual(len(self.r.headers()),1)
