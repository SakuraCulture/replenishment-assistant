import copy,sys,tempfile,threading,unittest
from pathlib import Path
from contextlib import closing
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from interaction_rules import validate_return_scope,configuration_changes
from test_archive_orders import seed
import archive_orders as ao,history_read as hr,work_repository as wr


class ScopeTests(unittest.TestCase):
    def test_return_exact_order_warehouse_revision(self):
        head={'oid':'one','revision':2,'warehouse':'河北仓'}
        plan={'plans':[{'head':dict(head),'payload':{'incoming':{'001':{'warehouse':'河北仓'}}}}]}
        before=copy.deepcopy(plan);validate_return_scope(plan,head);self.assertEqual(before,plan)
        for key,value in [('oid','other'),('revision',1)]:
            bad=copy.deepcopy(plan);bad['plans'][0]['head'][key]=value
            with self.assertRaises(ValueError):validate_return_scope(bad,head)
            validate_return_scope(bad,None)
        plan['plans'][0]['payload']['incoming']['002']={'warehouse':'山东仓'}
        with self.assertRaises(ValueError):validate_return_scope(plan,head)
    def test_parameter_diff_order_and_missing(self):
        rows=configuration_changes({'B':{'a':0,'b':None},'A':{'a':2,'c':''}},{'a':1,'b':None})
        self.assertEqual([r[0] for r in rows],['B','A','A','A']);self.assertEqual(rows[2][2:],['','未设置']);self.assertEqual(rows[3][2:],['未设置',None])


class BatchVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.work,self.repo,self.payload=seed(self.root)
    def tearDown(self):self.tmp.cleanup()
    def heads(self,trash=False):return hr.execute(self.work.path,self.root/'idx.db',{'kind':'list','trash':trash},threading.Event(),lambda _:None)['records']
    def snapshots(self):
        with closing(self.work.connect()) as db:return {t:db.execute('SELECT * FROM '+t).fetchall() for t in ('normal_orders','normal_revisions','normal_work_revisions','source_files')}
    def test_atomic_recycle_restore_same_snapshots(self):
        before=self.snapshots();heads=self.heads();ao.change_many(self.work.path,heads,True);self.assertEqual(self.heads(),[]);trash=self.heads(True)
        self.assertEqual([h['warehouse'] for h in trash],['河北仓','山东仓']);self.assertEqual(before,self.snapshots())
        ao.change_many(self.work.path,trash,False);self.assertEqual(len(self.heads()),2);self.assertEqual(before,self.snapshots())
    def test_second_conflict_rolls_back_first_and_events(self):
        heads=self.heads();heads[1]['visibility_token']=99
        with self.assertRaises(wr.Conflict):ao.change_many(self.work.path,heads,True)
        with closing(self.work.connect()) as db:self.assertEqual(ao.states(db),{});self.assertEqual(db.execute('SELECT COUNT(*) FROM normal_events').fetchone()[0],0)
    def test_cancel_duplicate_empty_rejected(self):
        heads=self.heads();cancel=threading.Event();cancel.set()
        with self.assertRaises(Exception):ao.change_many(self.work.path,heads,True,cancel=cancel)
        for selection in ([],[heads[0],heads[0]]):
            with self.assertRaises(ValueError):ao.change_many(self.work.path,selection,True)
        self.assertEqual(len(self.heads()),2)
