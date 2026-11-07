"""Deletion optimization must preserve bytes, checks and transaction boundaries."""
import copy,datetime,hashlib,sqlite3,sys,unittest,zlib
from contextlib import closing
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import data_maintenance as m,maintenance_storage as ms,storage_chunks as c,work_repository as wr
import test_data_maintenance as fixtures

class DeltaReferences(unittest.TestCase):
    setUp=fixtures.DeleteOrders.setUp
    def warm(self):
        with closing(sqlite3.connect(self.path)) as db,db:
            ms.cache_schema(db)
            for h,size,data in db.execute('SELECT hash,size,data FROM snapshot_chunks'):
                ms.references_for(db,h,size,data)
    def test_valid_source_receipts_are_not_copied_or_republished(self):
        self.warm();plan=m.prepare(self.path,self.heads)
        with closing(m.connect(self.path)) as db,closing(sqlite3.connect(':memory:')) as scratch:
            before=db.total_changes
            m.prepare_rewrites(db,plan,scratch,lambda _:None,None)
            self.assertEqual(db.total_changes,before)
            self.assertEqual(scratch.execute('SELECT COUNT(*) FROM maintenance_chunk_refs').fetchone()[0],0)
            self.assertEqual(scratch.execute('SELECT COUNT(*) FROM verified_chunks').fetchone()[0],db.execute('SELECT COUNT(*) FROM snapshot_chunks').fetchone()[0])
            ms.publish_cache(db,scratch);self.assertEqual(db.total_changes,before)
    def test_invalid_receipt_repair_is_staged_until_publication(self):
        self.warm();plan=m.prepare(self.path,self.heads)
        with closing(m.connect(self.path)) as db,db,closing(sqlite3.connect(':memory:')) as scratch:
            h=db.execute('SELECT hash FROM maintenance_chunk_refs LIMIT 1').fetchone()[0]
            db.execute("UPDATE maintenance_chunk_refs SET checksum='bad' WHERE hash=?",(h,))
            before=db.execute('SELECT * FROM maintenance_chunk_refs ORDER BY hash').fetchall()
            m.prepare_rewrites(db,plan,scratch,lambda _:None,None)
            self.assertEqual(db.execute('SELECT * FROM maintenance_chunk_refs ORDER BY hash').fetchall(),before)
            delta=scratch.execute('SELECT hash FROM maintenance_chunk_refs').fetchall()
            self.assertEqual(delta,[(h,)])
            ms.publish_cache(db,scratch)
            self.assertNotEqual(db.execute('SELECT checksum FROM maintenance_chunk_refs WHERE hash=?',(h,)).fetchone()[0],'bad')
    def test_external_cache_never_hides_corrupt_content(self):
        with closing(sqlite3.connect(':memory:')) as source,closing(sqlite3.connect(':memory:')) as scratch:
            for db in (source,scratch):ms.cache_schema(db)
            raw=wr.dumps({'sha':'a'*64}).encode();h=hashlib.sha256(raw).hexdigest()
            ms.references_for(source,h,len(raw),zlib.compress(raw))
            with self.assertRaisesRegex(ValueError,'校验失败'):
                ms.references_for(scratch,h,len(raw),zlib.compress(b'corrupt'),cache_source=source)
            self.assertEqual(scratch.execute('SELECT COUNT(*) FROM maintenance_chunk_refs').fetchone()[0],0)
    def test_cached_block_checks_each_reference_length_before_discard(self):
        import engine
        config=engine.config();field=config['sales_fields']['store_code']
        payload={'schema':1,'day':wr.today(),'model_version':'0.3.4-rules',
                 'states':{'T':{'name':'T','state':'待计算'},'U':{'name':'U','state':'待计算'}},
                 '_calculation_settings':{'T':field},'loaded':{'config':config}}
        self.w.save_job(wr.today(),payload,None)
        with closing(sqlite3.connect(self.path)) as db,db:
            manifest=c.require_manifest(db.execute('SELECT payload FROM jobs').fetchone()[0])
            node=dict(dict(manifest['tree'][1])['_calculation_settings'][1])['T']
            node[2]+=1
            corrupted=c.Prepared(manifest['tree'],manifest['digest'],manifest['size'],{}).blob()
            db.execute('UPDATE jobs SET payload=?',(corrupted,))
        plan=m.prepare(self.path,self.heads)
        with self.assertRaisesRegex(ValueError,'校验失败'):m.execute(self.path,plan)
        self.assertEqual(len(self.r.headers()),1)
        with closing(sqlite3.connect(self.path)) as db:self.assertEqual(db.execute('SELECT payload FROM jobs').fetchone()[0],corrupted)

class StreamRewrite(unittest.TestCase):
    def compare(self,value,codes={'T'}):
        expected=m.strip_work(value,codes)
        with closing(sqlite3.connect(':memory:')) as db,closing(sqlite3.connect(':memory:')) as scratch:
            blob=c.put(db,c.prepare(value));editor=ms.Editor(db,scratch);pack=editor.rewrite(blob,codes)
            self.assertEqual(wr.dumps(editor.value(pack.tree)),wr.dumps(expected))
            self.assertEqual((pack.digest,pack.size),(hashlib.sha256(wr.dumps(expected).encode()).hexdigest(),len(wr.dumps(expected).encode())))
    def test_many_blocks_with_deleted_and_retained_stores_and_optional_defaults(self):
        for count in (0,1,255,256,257,801):
            value={'states':{str(i):{'name':str(i),'flag':False} for i in range(600)},
                   'results':{'T':{'keep_even_when_removing':'must validate'},'U':{'n':0}},
                   'loaded':{'normalized':[({'B':'T' if i%3 else 'U'}, {'i':i,'date':datetime.date(2026,10,22),'none':None}) for i in range(count)],
                             'intake':{'frozen':True}},
                   'export_log':[{'nested':[{'store':'T'}]},{'nested':[{'store':'U'}]}],
                   'catalogs':{'T':'shared'}}
            with self.subTest(count=count):self.compare(value)
    def test_selected_intake_uses_saved_store_code_mapping(self):
        import engine
        config=copy.deepcopy(engine.config());config['sales_fields']['store_code']='CUSTOM'
        self.compare({'loaded':{'config':config,'intake':{'selected':[{'values':{'CUSTOM':' T '}},{'values':{'CUSTOM':'U'}}]}}})
    def test_wide_root_current_format_uses_compatible_bounded_path(self):
        self.compare({**{f'extra-{i}':i for i in range(300)},'states':{'T':1,'U':0},'pending_copy':None,'export_log':[]})
    def test_source_digest_mismatch_rejected_even_when_all_stores_removed(self):
        with closing(sqlite3.connect(':memory:')) as db,closing(sqlite3.connect(':memory:')) as scratch:
            pack=c.prepare({'states':{'T':{'name':'T'}},'pending_copy':{'secret':'must still check'}})
            blob=c.put(db,pack);pack.digest='0'*64
            with self.assertRaisesRegex(ValueError,'完整快照校验失败'):
                ms.Editor(db,scratch).rewrite(pack.blob(),{'T'})
    def test_corrupt_removed_branch_is_still_validated(self):
        with closing(sqlite3.connect(':memory:')) as db,closing(sqlite3.connect(':memory:')) as scratch:
            blob=c.put(db,c.prepare({'pending_copy':{'x':'retired content'},'states':{'T':0,'U':1}}))
            node=dict(c.manifest(blob)['tree'][1])['pending_copy'];h=dict(node[1])['x'][1]
            db.execute('UPDATE snapshot_chunks SET data=? WHERE hash=?',(zlib.compress(b'"tampered"'),h))
            with self.assertRaisesRegex(ValueError,'校验失败'):ms.Editor(db,scratch).rewrite(blob,{'T'})

if __name__=='__main__':unittest.main()
