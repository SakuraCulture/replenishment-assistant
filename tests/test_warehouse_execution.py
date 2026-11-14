"""Per-warehouse identity without changing supply routes or calculation data."""
import copy,sys,unittest
from pathlib import Path
from contextlib import closing
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import test_archive_orders as fixtures
import tempfile
import warehouse_execution as execution,normal_orders as normal,work_repository as wr


class WarehouseExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.work,self.repo,self.original=fixtures.seed(Path(self.temp.name))

    def heads(self):
        with closing(self.work.connect()) as db:return {h['warehouse']:h for h in execution.heads(db)}

    def materialize(self):
        with closing(self.work.connect()) as db,db:execution.ensure_parent(db,'oid')

    def next_source(self,selected):
        payload=copy.deepcopy(self.original)
        payload['result']['rows'][1]['D']='山东资料更新'
        digest=normal.fingerprint_owned(payload['result'],payload.get('new_focus'))
        with closing(self.work.connect()) as db,db:
            db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',('oid',2,digest,wr.now(),normal.encode(payload,db)))
            db.execute('UPDATE normal_orders SET current=2 WHERE oid=?',('oid',))
            execution.publish(db,'oid',2,digest,wr.now(),payload,selected)
        return payload

    def test_legacy_read_does_not_create_tables_or_change_snapshots(self):
        before=self.repo.get(wr.today(),'T');heads=self.heads()
        self.assertEqual(list(heads),['河北仓','山东仓'])
        with closing(self.work.connect()) as db:self.assertFalse(execution.exists(db))
        self.assertEqual(self.repo.get(wr.today(),'T'),before)

    def test_materialization_preserves_identity_and_freezes_only_warehouse_rows(self):
        old=self.heads();self.materialize();heads=self.heads()
        for warehouse,head in heads.items():
            self.assertEqual((head['digest'],head['revision']),(old[warehouse]['digest'],old[warehouse]['revision']))
            order=execution.get_order(self.repo,head)
            self.assertEqual({r['AU'] for r in order['payload']['result']['rows']},{warehouse})
            self.assertEqual(order['number'],'BH-T-ZC')
            self.assertEqual(head['source_parent_revision'],1)
            self.assertEqual(head['child_revision'],1)

    def test_sibling_source_revision_is_pinned_after_only_shandong_changes(self):
        self.materialize();before=self.heads();hb=execution.get_order(self.repo,before['河北仓'])
        self.next_source(['山东仓']);heads=self.heads()
        self.assertEqual(heads['河北仓'],before['河北仓'])
        self.assertEqual(execution.get_order(self.repo,heads['河北仓']),hb)
        self.assertEqual(heads['山东仓']['revision'],2)
        self.assertEqual(heads['山东仓']['child_revision'],2)
        with closing(self.work.connect()) as db:
            execution.validate(db,before['河北仓'])
            with self.assertRaises(wr.Conflict):execution.validate(db,before['山东仓'])

    def test_first_issue_only_one_warehouse_does_not_activate_the_other(self):
        with closing(self.work.connect()) as db,db:
            digest=db.execute('SELECT digest FROM normal_revisions').fetchone()[0]
            execution.publish(db,'oid',1,digest,wr.now(),self.original,['山东仓'])
        self.assertEqual(list(self.heads()),['山东仓'])
        with closing(self.work.connect()) as db:
            self.assertFalse(execution.is_active(db,'oid','河北仓'))
            self.assertTrue(execution.needs_publish(db,'oid',digest,['河北仓']))
            execution.publish(db,'oid',1,digest,wr.now(),self.original,['河北仓']);db.commit()
        self.assertEqual(set(self.heads()),{'山东仓','河北仓'})
        self.assertEqual(wr.dumps(self.original['result']['warehouses']),wr.dumps(self.repo.get(wr.today(),'T')['payload']['result']['warehouses']))

    def test_other_warehouse_generation_does_not_block_a_changed_sibling(self):
        self.materialize()
        import jst_orders
        with closing(self.work.connect()) as db,db:
            jst_orders.schema(db);db.execute('INSERT INTO jst_active VALUES(?,?,?)',('oid','河北仓','existing-hb'))
        self.next_source(['山东仓'])
        self.assertEqual(self.heads()['河北仓']['revision'],1)

    def test_snapshot_integrity_checked_independently_of_legacy_parent_digest(self):
        self.materialize();head=self.heads()['河北仓']
        with closing(self.work.connect()) as db,db:
            db.execute('UPDATE warehouse_execution_revisions SET snapshot_digest=? WHERE oid=? AND warehouse=?',('wrong','oid','河北仓'))
        with self.assertRaisesRegex(ValueError,'指纹|快照'):
            execution.get_order(self.repo,head)

    def test_repeated_publish_does_not_increment_child_revision(self):
        self.materialize();before=self.heads()
        with closing(self.work.connect()) as db,db:
            execution.publish(db,'oid',1,before['河北仓']['digest'],before['河北仓']['saved'],self.original,['河北仓'])
        self.assertEqual(self.heads(),before)

    def test_retired_warehouse_is_not_revived_by_parent_or_default_selection(self):
        self.materialize();before=self.heads()['山东仓']
        with closing(self.work.connect()) as db,db:execution.retire(db,'oid','河北仓','test-operation')
        self.assertEqual(list(self.heads()),['山东仓'])
        self.assertEqual(self.heads()['山东仓'],before)
        with closing(self.work.connect()) as db:
            with self.assertRaises(wr.Conflict):execution.publish(db,'oid',1,before['digest'],wr.now(),self.original,['河北仓'])


if __name__=='__main__':unittest.main()
