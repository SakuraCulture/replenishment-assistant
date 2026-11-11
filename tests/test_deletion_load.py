"""Bounded deletion retains the exact preview/CAS/rollback and shared-file rules."""
import datetime,hashlib,io,sqlite3,sys,threading,unittest,zlib
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import data_maintenance as m,maintenance_storage as ms,storage_chunks as chunks,work_repository as wr
import test_data_maintenance as fixtures

class StreamingEvidence(unittest.TestCase):
    def test_exact_canonical_list_token_and_non_destructive_finalization(self):
        rows=[['中文',None,0,0.0,False,'',{'z':datetime.date(2026,11,6),'a':'\n"'}],['next',list(range(40))]]
        d=ms.EvidenceDigest();self.assertEqual(d.hexdigest(),m.digest([]))
        for row in rows:d.append(row)
        self.assertEqual(d.hexdigest(),m.digest(rows));self.assertEqual(d.hexdigest(),m.digest(rows))
        d.extend(iter([['tail']]));self.assertEqual(d.hexdigest(),m.digest(rows+[['tail']]))
    def test_utf8_boundary_and_invalid_tail(self):
        text='x'*262143+'中'+'f'*64+'!'
        self.assertEqual(ms.hashes(text),['f'*64])
        with self.assertRaises(UnicodeDecodeError):ms.hashes(text.encode()+b'\xff')
        with self.assertRaises(UnicodeDecodeError):ms.hashes(b'\xe4\xb8')
    def test_cache_publication_changes_only_new_or_changed_rows(self):
        with closing(sqlite3.connect(':memory:')) as db,closing(sqlite3.connect(':memory:')) as scratch:
            for c in (db,scratch):ms.cache_schema(c);c.execute('INSERT INTO maintenance_chunk_refs VALUES(?,?,?,?)',('h','c','[]','s'))
            before=db.total_changes;ms.publish_cache(db,scratch);self.assertEqual(before,db.total_changes)
            scratch.execute("UPDATE maintenance_chunk_refs SET refs='[1]'");ms.publish_cache(db,scratch)
            self.assertEqual(db.total_changes,before+1)
            scratch.execute("INSERT INTO maintenance_chunk_refs VALUES('h2','c','[]','s')");ms.publish_cache(db,scratch)
            self.assertEqual(db.total_changes,before+2)
    def test_index_scan_preserves_matches_boundaries_and_rejects_invalid_text(self):
        needle=wr.dumps('待删-oid');text='x'*262141+needle+'中'*300000
        self.assertTrue(ms.contains_text(io.BytesIO(text.encode()),[needle]))
        self.assertFalse(ms.contains_text(io.BytesIO(text.encode()),[wr.dumps('另一个')]))
        with self.assertRaises(UnicodeDecodeError):ms.contains_text(io.BytesIO(text.encode()+b'\xe4'),[needle])
        event=threading.Event();event.set()
        from runtime_support import Cancelled
        with self.assertRaises(Cancelled):ms.contains_text(io.BytesIO(b'{}'),[needle],event)
    def test_large_cache_rows_keep_exact_unselected_bytes(self):
        with closing(sqlite3.connect(':memory:')) as db:
            db.execute("ATTACH DATABASE ':memory:' AS cache0");db.execute('CREATE TABLE cache0.views(k TEXT PRIMARY KEY,projection TEXT)')
            rows=[('key-'+wr.dumps('oid'),'{}'),('payload-ref','x'*262141+wr.dumps('oid')+'中'*300000),('keep','x'*300000+wr.dumps('other'))]
            db.executemany('INSERT INTO cache0.views VALUES(?,?)',rows)
            m.clear_indexes(db,{'oid'},lambda _:None,None)
            self.assertEqual(db.execute('SELECT * FROM cache0.views').fetchall(),[rows[2]])

class VerifiedReferences(unittest.TestCase):
    setUp=fixtures.DeleteOrders.setUp
    def keep_job(self):
        p={'schema':1,'day':'2001-01-01','model_version':'0.3.4-rules','states':{'U':{'name':'keep','state':'待计算'}},'unknown':{'proof':'kept'}}
        self.w.save_job('2001-01-01',p,None)
        with closing(sqlite3.connect(self.path)) as db:
            blob=db.execute("SELECT payload FROM jobs WHERE day='2001-01-01'").fetchone()[0]
            h=next(v[1] for k,v in chunks.manifest(blob)['tree'][1] if k=='unknown' for k,v in v[1])
        return h
    def test_stable_database_reuses_only_operation_verified_references(self):
        self.keep_job();plan=m.prepare(self.path,self.heads);old=ms.references_for;phase=['prepare'];calls=[]
        gc=m.collect_shared
        def collect(*args,**kwargs):
            phase[0]='collect';self.assertIsNotNone(kwargs['verified']);return gc(*args,**kwargs)
        def refs(*args,**kwargs):calls.append(phase[0]);return old(*args,**kwargs)
        with patch.object(m,'collect_shared',side_effect=collect),patch.object(ms,'references_for',side_effect=refs):m.execute(self.path,plan)
        self.assertIn('prepare',calls);self.assertNotIn('collect',calls)
        self.assertIsNotNone(self.w.get_job('2001-01-01'))
    def test_concurrent_commit_discards_shortcut_and_revalidates_bytes(self):
        self.keep_job();plan=m.prepare(self.path,self.heads);original=m.prepare_rewrites;gc=m.collect_shared
        def prepare(*args):
            result=original(*args)
            if not args[0].in_transaction:
                with closing(sqlite3.connect(self.path)) as other,other:other.execute("INSERT INTO stores VALUES('other','{}')")
            return result
        def collect(*args,**kwargs):
            self.assertIsNone(kwargs['verified']);return gc(*args,**kwargs)
        with patch.object(m,'prepare_rewrites',side_effect=prepare),patch.object(m,'collect_shared',side_effect=collect):m.execute(self.path,plan)
        self.assertIsNotNone(self.w.get_job('2001-01-01'))
    def test_concurrent_corruption_not_hidden_by_verified_cache(self):
        h=self.keep_job();plan=m.prepare(self.path,self.heads);original=m.prepare_rewrites
        def prepare(*args):
            result=original(*args)
            with closing(sqlite3.connect(self.path)) as other,other:other.execute('UPDATE snapshot_chunks SET data=? WHERE hash=?',(zlib.compress(b'"corrupt"'),h))
            return result
        with patch.object(m,'prepare_rewrites',side_effect=prepare),self.assertRaisesRegex(ValueError,'校验失败'):m.execute(self.path,plan)
        self.assertEqual(len(self.r.headers()),1)
    def test_prepared_descriptors_do_not_retain_source_blobs(self):
        plan=m.prepare(self.path,self.heads)
        with closing(m.connect(self.path)) as db,closing(sqlite3.connect(':memory:')) as scratch:
            prepared=m.prepare_rewrites(db,plan,scratch,lambda _:None,None)
            self.assertTrue(prepared)
            for _,_,token,_,_ in prepared:self.assertIsInstance(token,bytes);self.assertEqual(len(token),32)
    def test_valid_shared_payload_change_after_prepare_is_rebased(self):
        plan=m.prepare(self.path,self.heads);original=m.prepare_rewrites
        def prepare(*args):
            result=original(*args)
            if not args[0].in_transaction:
                with closing(sqlite3.connect(self.path)) as other,other:
                    import normal_orders
                    other.execute('UPDATE normal_work_revisions SET payload=?',(normal_orders.encode({'new':'content'}, other),))
            return result
        with patch.object(m,'prepare_rewrites',side_effect=prepare):m.execute(self.path,plan)
        self.assertEqual(self.r.headers(),[])

class DuplicateWorkRewrites(unittest.TestCase):
    setUp=fixtures.DeleteOrders.setUp

    def duplicate_draft(self):
        day=wr.today()
        value={'schema':1,'day':day,'model_version':'0.3.4-rules',
               'states':{'T':{'name':'remove','state':'待计算'},
                         'U':{'name':'keep','state':'待计算'}},
               'results':{},'pending_copy':None,'export_log':[]}
        self.w.save_job(day,value,None)
        with closing(sqlite3.connect(self.path)) as db,db:
            blob=db.execute('SELECT payload FROM jobs WHERE day=?',(day,)).fetchone()[0]
            db.execute('UPDATE normal_work_revisions SET payload=?',(blob,))
        return value

    def test_duplicate_draft_and_signed_work_keep_exact_unselected_values(self):
        value=self.duplicate_draft()
        plan=m.prepare(self.path,self.heads)
        original=ms.Editor.rewrite
        with closing(m.connect(self.path)) as db,closing(sqlite3.connect(':memory:')) as scratch:
            with patch.object(ms.Editor,'rewrite',autospec=True,side_effect=original) as rewrite:
                prepared=m.prepare_rewrites(db,plan,scratch,lambda _:None,None)
            self.assertEqual(rewrite.call_count,1)
            self.assertEqual(len(prepared),2)
            editor=ms.Editor(db,scratch)
            expected=wr.dumps(m.strip_work(value,{'T'}))
            for _,_,_,pack,_ in prepared:
                self.assertEqual(wr.dumps(editor.value(pack.tree)),expected)

    def test_duplicate_payload_still_checks_each_draft_digest(self):
        self.duplicate_draft()
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute("UPDATE jobs SET digest='invalid'")
        plan=m.prepare(self.path,self.heads)
        with self.assertRaisesRegex(ValueError,'校验|不一致'):m.execute(self.path,plan)
        self.assertEqual(len(self.r.headers()),1)

    def test_same_payload_on_different_days_uses_each_days_selected_stores(self):
        value=self.duplicate_draft()
        with closing(m.connect(self.path)) as db,db,closing(sqlite3.connect(':memory:')) as scratch:
            blob=db.execute('SELECT payload FROM normal_work_revisions').fetchone()[0]
            db.execute('INSERT INTO normal_work_revisions VALUES(?,?,?)',('other-day','2001-01-01',blob))
            plan={'parents':[{'day':wr.today(),'store':'T'},{'day':'2001-01-01','store':'U'}]}
            prepared=m.prepare_rewrites(db,plan,scratch,lambda _:None,None)
            editor=ms.Editor(db,scratch)
            for _,key,_,pack,_ in prepared:
                removed={'U'} if key=='other-day' else {'T'}
                self.assertEqual(wr.dumps(editor.value(pack.tree)),wr.dumps(m.strip_work(value,removed)))

    def test_repeated_staged_block_is_compressed_once(self):
        with closing(sqlite3.connect(':memory:')) as db,closing(sqlite3.connect(':memory:')) as scratch:
            editor=ms.Editor(db,scratch)
            with patch.object(ms.zlib,'compress',wraps=zlib.compress) as compress:
                first=editor.block({'keep':[None,0,False,'中文']})
                second=editor.block({'keep':[None,0,False,'中文']})
            self.assertEqual(first,second)
            self.assertEqual(compress.call_count,1)
            self.assertEqual(editor.value(second),{'keep':[None,0,False,'中文']})

if __name__=='__main__':unittest.main()
