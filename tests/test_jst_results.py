"""Persistent result/export contracts against isolated real SQLite snapshots."""
import copy,sys,sqlite3,threading,unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).resolve().parents[1]/'tools')]
import jst_results as results
import jst_orders as orders
import jst_exports
import work_repository as wr
import order_lifecycle as lc
import test_jst


def facts(path):
    with closing(sqlite3.connect(path)) as db:
        return {n:db.execute('SELECT * FROM "'+n+'"').fetchall() for n, in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='jst_events'")}


def cells(path):
    import openpyxl
    b=openpyxl.load_workbook(path);value=[(s.title,s.freeze_panes,[[[c.value,c.data_type,c.number_format,str(c._style)] for c in row] for row in s]) for s in b];b.close();return value


class Results(unittest.TestCase):
    def setUp(self):
        self.f=test_jst.Service();self.f.setUp();self.path=self.f.path;self.root=self.f.f.root;self.h=self.f.h;self.cache=self.root/'result-cache.sqlite3'
    def tearDown(self):self.f.tearDown()
    def read(self):return results.read(self.path,self.cache,self.h['day'])
    def test_repeat_exact_export_keeps_business_facts_and_ready_rows(self):
        p,gid=self.f.generated();before=facts(self.path);data=self.read();self.assertEqual(facts(self.path),before)
        self.assertEqual(data['counts']['ready'],1);self.assertEqual(data['counts']['waiting'],1)
        plan=results.export_plan(data);r=results.export(self.path,plan,self.root/'out');first=cells(r['files'][0]['path'])
        again=self.read();self.assertNotEqual(next(r for r in again['records'] if r['gid'])['last_export'],'未导出')
        r2=results.export(self.path,results.export_plan(again),self.root/'out')
        self.assertEqual(cells(r2['files'][0]['path']),first);self.assertEqual(r['files'][0]['path'],r2['files'][0]['path'])
        self.assertEqual(facts(self.path),before);self.assertEqual(self.f.balances(),60)
    def test_locked_paid_received_orders_remain_exportable(self):
        _,gid=self.f.generated();orders.lock(self.path,[gid]);self.f.refresh(60)
        self.f.f.save('payment');self.f.f.save('receipt');data=self.read()
        self.assertEqual(data['counts']['ready'],1);self.assertTrue(next(r for r in data['records'] if r['gid'])['locked'])
        self.assertEqual(results.export_plan(data)['entries'][0]['gid'],gid)
    def test_stale_return_refresh_and_explicit_old_export(self):
        _,gid=self.f.generated();plan=results.export_plan(self.read());self.f.f.save('return',[{'门店确认数量':30}])
        with self.assertRaisesRegex(wr.Conflict,'更新'):results.export(self.path,plan,self.root/'out')
        self.assertFalse((self.root/'out').exists());data=self.read();self.assertEqual(data['counts']['stale'],1)
        self.assertEqual(results.export_plan(data)['entries'],[])
        old=results.export_plan(data,allow_old=True);r=results.export(self.path,old,self.root/'old')
        self.assertEqual(cells(r['files'][0]['path'])[0][2][1][2][0],40)
        correction=orders.prepare(self.path,[self.h],correction=True);orders.commit(self.path,correction)
        with self.assertRaises(wr.Conflict):results.export(self.path,old,self.root/'old')
    def test_inventory_refresh_requires_review_only_unlocked(self):
        _,gid=self.f.generated();self.f.refresh(90);data=self.read();self.assertEqual(data['counts']['stale'],1)
        orders.lock(self.path,[gid]);self.assertEqual(self.read()['counts']['ready'],1)
    def test_store_return_change_invalidates_export_even_with_same_operations_version(self):
        import operations_flow
        operations_flow.adopt(self.path,[self.h])
        _,gid=self.f.generated();plan=results.export_plan(self.read())
        r=plan['entries'][0]
        self.f.f.save('return',[{'门店确认数量':20}])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(results.return_token(db,self.h),r['return_token'])
        for export in (lambda:results.export(self.path,plan,self.root/'out'),
                       lambda:jst_exports.export_checked(self.path,[gid],self.root/'out')):
            with self.assertRaisesRegex(wr.Conflict,'门店回传或运营签发依据已更新'):
                export()
        self.assertFalse((self.root/'out').exists())

    def test_new_file_survives_old_export_finishing_late(self):
        _,gid=self.f.generated();plan=results.export_plan(self.read())
        original=jst_exports.openpyxl.Workbook.save;folder=self.root/'concurrent';new=[]
        def staged(book,path):
            original(book,path)
            if not Path(path).name.startswith('.jst-') or new:return
            self.f.f.save('return',[{'门店确认数量':20}])
            new.extend(orders.commit(self.path,orders.prepare(self.path,[self.h],correction=True))['generated'])
            self.assertEqual(len(new),1)
            current=jst_exports.export_checked(self.path,new,folder)
            self.assertFalse(current['failed'])
        with patch.object(jst_exports.openpyxl.Workbook,'save',staged):
            r=results.export(self.path,plan,folder)
        self.assertEqual(r['files'],[]);self.assertEqual(r['remaining'],[gid])
        self.assertIn(self.h['number'],r['failed'][0]['error'])
        self.assertIn('生成记录或仓资料已更新',r['failed'][0]['error'])
        files=list(folder.glob('*.xlsx'));self.assertEqual(len(files),1)
        self.assertEqual(cells(files[0])[0][2][1][2][0],20)
        self.assertEqual(list(folder.glob('.jst-*')),[])
        current=results.export_plan(self.read())
        self.assertEqual(current['entries'][0]['gid'],new[0])
    def test_legacy_return_and_paid_history_can_export(self):
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute("DELETE FROM lifecycle_versions WHERE stage='return'")
            db.execute('CREATE TABLE merchant_feedback(oid TEXT,revision INTEGER,version INTEGER,saved TEXT,payload TEXT)')
            db.execute('INSERT INTO merchant_feedback VALUES(?,?,?,?,?)',('oid',1,1,wr.now(),wr.dumps({'values':{'0001':40},'warehouses':{}})))
        _,gid=self.f.generated();orders.lock(self.path,[gid]);lc.commit(self.path,orders.payment_plan(self.path,gid))
        data=self.read();self.assertEqual(data['counts']['ready'],1)
        r=results.export(self.path,results.export_plan(data),self.root/'legacy')
        self.assertEqual(r['files'][0]['gid'],gid)
    def test_zero_result_and_return_zero_stay_visible(self):
        self.f.refresh(0);orders.commit(self.path,self.f.plan());data=self.read()
        self.assertEqual(data['counts']['zero'],1);self.assertEqual(data['metrics']['allocated'],0);self.assertEqual(data['metrics']['reduced'],80)
        self.assertEqual(results.export_plan(data)['entries'],[])
        self.f.f.save('return',[{'门店确认数量':0}]);self.assertEqual(self.read()['counts']['zero'],1)
    def test_warm_projection_does_not_decode_generation_and_tamper_rebuilt(self):
        self.f.generated();data=self.read()
        with patch('jst_order_store.decode',side_effect=AssertionError('warm snapshot read')):self.assertEqual(self.read(),data)
        with closing(sqlite3.connect(self.cache)) as db,db:db.execute("UPDATE jst_result_projection SET payload='broken'")
        self.assertEqual(self.read(),data)

    def test_upward_projection_and_warm_cache_keep_separate_increase(self):
        self.f.f.save('return',[{'门店确认数量':1}])
        import jst_batches as batches
        p=batches.preview_new(self.path,[self.h],{'河北仓':test_jst.catalog(stock=100,step=24)})
        result=batches.execute(self.path,p);self.assertFalse(result['errors'])
        data=self.read();m=data['metrics']
        self.assertEqual((m['demand'],m['allocated'],m['reduced'],m['increased']),(2,48,0,46))
        self.assertEqual(m['reduced_sku'],0)
        with patch('jst_order_store.decode',side_effect=AssertionError('warm snapshot read')):
            self.assertEqual(self.read(),data)

    def test_projection_cache_v3_is_rebuilt_without_changing_generation(self):
        _,gid=self.f.generated();before=orders.get(self.path,gid);data=self.read()
        # Old cache entries are display caches, not business facts. A valid old
        # cache digest must not bypass the new gross reduction/increase split.
        with closing(sqlite3.connect(self.cache)) as db,db:
            rows=db.execute('SELECT k,payload FROM jst_result_projection').fetchall()
            import hashlib
            for key,raw in rows:
                key=wr.loads(key);key[0]=3;value=wr.loads(raw)
                value['amounts'].pop('increased');value['amounts']['reduced']='-999'
                raw=wr.dumps(value)
                db.execute('INSERT OR REPLACE INTO jst_result_projection VALUES(?,?,?)',(wr.dumps(key),raw,hashlib.sha256(raw.encode()).hexdigest()))
            db.execute('DELETE FROM jst_result_projection WHERE k LIKE ?',('[4,%',))
        self.assertEqual(self.read(),data);self.assertEqual(orders.get(self.path,gid),before)
    def test_scope_and_cancel_do_not_create_exports(self):
        self.f.generated();data=results.read(self.path,self.cache,self.h['day'],[results.key(self.h)])
        self.assertEqual(len(data['records']),1);self.assertEqual(results.export_plan(data,[])['entries'],[])
        event=threading.Event();event.set()
        from runtime_support import Cancelled
        with self.assertRaises(Cancelled):results.export(self.path,results.export_plan(data),self.root/'out',cancel=event)
        self.assertFalse((self.root/'out').exists())


class BatchResults(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile,jst_qa,warehouse_catalog
        cls.tmp=tempfile.TemporaryDirectory();cls.root=Path(cls.tmp.name);cls.path=jst_qa.fixture(cls.root/'seed',31)
        cls.heads=lc.current_targets(cls.path);cls.day=cls.heads[0]['day'];cls.cache=cls.root/'cache.sqlite3'
        for wh,c in warehouse_catalog.Repository(cls.path.parent).catalogs().items():
            for row in c['items'].values():row['可用数']=48*31
            c['revision']=warehouse_catalog.revision(c['items']);orders.commit_refresh(cls.path,orders.prepare_refresh(cls.path,wh,catalog=c))
        cls.plan=orders.prepare(cls.path,cls.heads);cls.receipt=orders.commit(cls.path,cls.plan)
    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()
    def read(self):return results.read(self.path,self.cache,self.day)
    def test_62_orders_no_page_cutoff_grouped_stores_keep_generation_order(self):
        data=self.read();self.assertEqual(data['counts']['ready'],62);self.assertEqual(len(data['groups']),31)
        self.assertEqual([r['gid'] for r in results.export_plan(data)['entries']],self.receipt['generated'])
        picked=[results.key(data['records'][0]['head']),results.key(data['records'][50]['head'])]
        self.assertEqual([results.key(r['head']) for r in results.export_plan(data,picked)['entries']],picked)
    def test_partial_failure_and_cancel_retry_exact_original_ids(self):
        data=self.read();plan=results.export_plan(data);plan['entries']=plan['entries'][:2];before=facts(self.path)
        replace=jst_exports.os.replace;count=[0]
        def fail_once(src,dst):
            count[0]+=1
            if count[0]==2:raise PermissionError('合成Excel占用')
            return replace(src,dst)
        with patch('jst_exports.os.replace',side_effect=fail_once):r=results.export(self.path,plan,self.root/'partial')
        self.assertEqual(len(r['files']),1);self.assertEqual(r['remaining'],[plan['entries'][1]['gid']])
        retry=copy.deepcopy(plan);retry['entries']=retry['entries'][1:];self.assertFalse(results.export(self.path,retry,self.root/'partial')['remaining'])
        event=threading.Event();r=results.export(self.path,plan,self.root/'cancel',lambda _:event.set(),event)
        self.assertEqual(len(r['files']),1);self.assertEqual(r['remaining'],[plan['entries'][1]['gid']]);self.assertTrue(r['cancelled'])
        event.clear();self.assertFalse(results.export(self.path,plan,self.root/'cancel',cancel=event)['remaining']);self.assertEqual(facts(self.path),before)


if __name__=='__main__':unittest.main()
