import copy,sqlite3,sys,tempfile,threading,unittest
from pathlib import Path
from contextlib import closing
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from test_archive_orders import seed
import data_maintenance as m
import purchases,normal_orders as no,work_repository as wr,storage_chunks as chunks
from runtime_support import Cancelled

class DeleteOrders(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup)
        self.root=Path(self.t.name);self.w,self.r,self.p=seed(self.root);self.path=self.w.path
        self.heads=purchases.targets(self.path)
    def test_preview_expands_parent_and_delete_reclaims_unowned_chunks(self):
        plan=m.prepare(self.path,[self.heads[0]])
        self.assertEqual(set(plan['parents'][0]['warehouses']),{'山东仓','河北仓'})
        m.execute(self.path,plan)
        self.assertEqual(purchases.targets(self.path),[]);self.assertEqual(self.r.headers(),[])
        with closing(sqlite3.connect(self.path)) as db:
            for table in ('normal_revisions','normal_work_revisions','source_files','snapshot_chunks'):
                self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0],0,table)
    def test_cancellation_rolls_back_no_recycle_bin(self):
        plan=m.prepare(self.path,self.heads);before=self.r.get(wr.today(),'T');cancel=threading.Event()
        def progress(s):
            if '正在删除' in str(s):cancel.set()
        with self.assertRaises(Cancelled):m.execute(self.path,plan,progress=progress,cancel=cancel)
        self.assertEqual(self.r.get(wr.today(),'T'),before)
        m.execute(self.path,plan);self.assertEqual(self.r.headers(),[])
    def test_unrelated_audit_progress_does_not_invalidate_confirmation(self):
        plan=m.prepare(self.path,self.heads)
        with closing(sqlite3.connect(self.path)) as db,db:db.execute('INSERT INTO normal_events(at,oid,revision,action,detail) VALUES(?,?,?,?,?)',(wr.now(),'oid',1,'changed','{}'))
        m.execute(self.path,plan)
        self.assertEqual(self.r.headers(),[])
    def test_selected_revision_change_rejects_entire_delete(self):
        plan=m.prepare(self.path,self.heads)
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('INSERT INTO normal_revisions SELECT oid,2,digest,saved,payload FROM normal_revisions WHERE revision=1')
            db.execute('UPDATE normal_orders SET current=2')
        with self.assertRaises(wr.Conflict):m.execute(self.path,plan)
        self.assertEqual(len(self.r.headers()),1)
    def test_other_store_unchanged_and_shared_source_kept(self):
        p=copy.deepcopy(self.p);p['result']['store']='U';p['result']['store_name']='U'
        for d in p['result']['rows']:d['B']='U'
        sha=no.fingerprint(p['result'])
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(wr.today(),'U','normal_model','other','BH-U-ZC',1))
            db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',('other',1,sha,wr.now(),no.encode(p, db)))
        before=self.r.get(wr.today(),'U')
        with closing(sqlite3.connect(self.path)) as db:sources=db.execute('SELECT * FROM source_files ORDER BY sha').fetchall()
        m.execute(self.path,m.prepare(self.path,self.heads))
        self.assertEqual(self.r.get(wr.today(),'U'),before)
        with closing(sqlite3.connect(self.path)) as db:self.assertEqual(db.execute('SELECT * FROM source_files ORDER BY sha').fetchall(),sources)
    def test_old_recycle_records_can_be_permanently_removed(self):
        import archive_orders as ao
        ao.change_many(self.path,self.heads,True)
        m.execute(self.path,m.prepare(self.path,self.heads))
        with closing(sqlite3.connect(self.path)) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM archive_visibility').fetchone()[0],0)
    def test_stale_cache_cannot_reopen_removed_order(self):
        import history_read
        cache=self.root/'stale-index.sqlite3';head=self.r.head(wr.today(),'T')
        history_read.cached(self.path,cache,head,True)
        m.execute(self.path,m.prepare(self.path,self.heads))
        with self.assertRaisesRegex(ValueError,'删除'):history_read.cached(self.path,cache,head,True)
    def test_pending_generation_task_is_deleted_even_without_inventory_tables(self):
        import jst_tasks
        with closing(sqlite3.connect(self.path)) as db,db:
            jst_tasks.schema(db)
            db.execute('INSERT INTO jst_tasks VALUES(?,?,?,?,?,?,?)',('pending','now',wr.dumps(self.heads[0]),'{}','pending',None,''))
        m.execute(self.path,m.prepare(self.path,self.heads))
        self.assertEqual(jst_tasks.pending(self.path),[])
    def test_cross_database_failure_rolls_back_both(self):
        from aoxiang_workflows import Workflows
        from unittest.mock import patch
        w=Workflows(self.path.parent/'aoxiang');job=w.prepare([self.heads[0]])
        w.update(job['id'],state='COMPLETED',position=0,changes={'state':'BOUND','package':{'id':'p1'}})
        plan=m.prepare(self.path,self.heads);before=w.get(job['id'])
        with patch('data_maintenance.collect_shared',side_effect=RuntimeError('disk error')):
            with self.assertRaises(RuntimeError):m.execute(self.path,plan,True)
        self.assertEqual(w.get(job['id']),before);self.assertEqual(len(self.r.headers()),1)
        m.execute(self.path,plan,True);self.assertEqual(w.list(),[])

class StockDeletion(unittest.TestCase):
    def setUp(self):
        from test_jst import Service
        self.s=Service();self.s.setUp();self.addCleanup(self.s.tearDown)
    def test_unplaced_releases_stock(self):
        self.s.generated();m.execute(self.s.path,m.prepare(self.s.path,[self.s.h]));self.assertEqual(self.s.balances(),100)
    def test_locked_does_not_add_stock_and_fresh_catalog_does_not_double_deduct(self):
        import jst_orders as j
        _,gid=self.s.generated();j.lock(self.s.path,[gid]);plan=m.prepare(self.s.path,[self.s.h])
        with self.assertRaises(ValueError):m.execute(self.s.path,plan)
        m.execute(self.s.path,plan,True);self.assertEqual(self.s.balances(),60)
        self.s.refresh(60);self.assertEqual(self.s.balances(),60)

if __name__=='__main__':unittest.main()
