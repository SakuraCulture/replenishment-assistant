import copy,hashlib,sqlite3,sys,tempfile,unittest
from contextlib import closing
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/p) for p in ('src','tests','tools')]
import openpyxl,order_lifecycle as lc,lifecycle_batch as batch,parameter_presets as pp,work_repository as wr
from store_parameter_import import base


class BusinessImportTests(unittest.TestCase):
    def test_verification_column_and_footer_keep_version_and_dependencies(self):
        from merge_fixture import Order
        import normal_exports
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);order=Order(root/'order','G');source=root/'original.xlsx';normal_exports.export(order.signed(),source,warehouse='河北仓')
            def prepare(path,stage='return'):
                read=batch.read([path],stage);self.assertFalse(read['issues'],read['issues']);return batch.prepare(order.path,read['documents'])
            lc.commit(order.path,prepare(source));lc.commit(order.path,prepare(source,'operations'))
            with closing(sqlite3.connect(order.path)) as db:
                previous=lc.current(db,order.h,'return');operations=lc.current(db,order.h,'operations')
            edited=root/'verification.xlsx';w=openpyxl.load_workbook(source)
            for s in w:
                if s.title=='系统关联信息':continue
                col=s.max_column+1;s.cell(1,col,'自行验算')
                for n in range(2,s.max_row+3):s.cell(n,col,'=1+2')
            w.save(edited);w.close();plan=prepare(edited);self.assertFalse(plan['plans'][0]['changed'])
            import lifecycle_recovery
            from unittest.mock import patch
            with patch.object(lc,'_commit_once',side_effect=RuntimeError('isolated-before-commit')):
                with self.assertRaisesRegex(RuntimeError,'isolated-before-commit'):lc.commit(order.path,plan)
            recovery=lifecycle_recovery.recent(order.path)[0];self.assertEqual(lifecycle_recovery.inspect(order.path,recovery['id'])['action'],'retry')
            result=lifecycle_recovery.recover(order.path,recovery['id'])['receipt'];self.assertEqual(result[0]['version'],1)
            self.assertEqual(lifecycle_recovery.inspect(order.path,recovery['id'])['action'],'completed')
            lc.commit(order.path,prepare(edited))
            with closing(sqlite3.connect(order.path)) as db:
                self.assertEqual(lc.current(db,order.h,'return'),previous);self.assertEqual(lc.current(db,order.h,'operations'),operations)
                self.assertEqual(db.execute("SELECT count(*) FROM lifecycle_events WHERE action='业务未变化，留存新原件'").fetchone()[0],1)
            docs=lc.history(order.path,order.h,'return');self.assertEqual(len(docs[0]['attachments']),2)
            sha=hashlib.sha256(edited.read_bytes()).hexdigest();target=root/'download.xlsx';lc.attachment(order.path,order.h,'return',1,sha,target)
            self.assertEqual(target.read_bytes(),edited.read_bytes())
            import data_maintenance,order_merge
            with closing(sqlite3.connect(order.path)) as db,db:data_maintenance.collect_shared(db,order.root)
            restored=root/'after-cleanup.xlsx';lc.attachment(order.path,order.h,'return',1,sha,restored);self.assertEqual(restored.read_bytes(),edited.read_bytes())
            graph=order_merge.Graph(order.root,lambda _:None,None)
            try:
                asset=next(r for r in graph.rows if r.table=='lifecycle_files' and r.values['sha']==sha)
                self.assertIn(order.oid,asset.owners)
            finally:graph.close()
            # Mapped quantity edits continue to produce a real business revision.
            scan=copy.deepcopy(batch.read([source],'return')['documents'][0]['scan']);scan['rows'][0]['quantity']='7';scan['rows'][0]['base_quantity']='7'
            actual=lc.prepare_numbered(order.path,scan,scope=order.h);self.assertTrue(actual['plans'][0]['changed'])
    def test_incomplete_business_row_is_still_rejected(self):
        from test_lifecycle import LifecycleTests
        f=LifecycleTests();f.setUp()
        try:
            with self.assertRaisesRegex(ValueError,'编码缺失'):f.scan(items=[{'商品编码':None,'门店确认数量':3}])
        finally:f.tearDown()


class ScopedPresetTests(unittest.TestCase):
    def test_unrelated_edit_passes_and_new_reference_conflicts(self):
        import parameter_schema
        with tempfile.TemporaryDirectory() as temp:
            repo=wr.Repository(temp);repo.save_stores({'A':base(),'B':base()});p=pp.save(repo,'P',parameter_schema.standard());pp.assign(repo,['A'],p['id'],pp.snapshot(repo));snapshot=pp.snapshot(repo)
            b=repo.stores()['B'];edited=copy.deepcopy(b);edited['ui'].update(mode=2,amount='200');repo.save_stores({'B':edited},expected={'B':b})
            pp.save(repo,'P2',p['config'],p['id'],snapshot)
            expected=pp.snapshot(repo);pp.assign(repo,['B'],p['id'],expected)
            with self.assertRaises(wr.Conflict):pp.save(repo,'stale',p['config'],p['id'],expected)


class ScopedPaymentTests(unittest.TestCase):
    def test_different_company_same_name_and_store_does_not_invalidate(self):
        import test_payment_matching,payment_matching as pm,jst_orders as jst,payment_shop_names as names
        for configured in (False,True):
            with self.subTest(configured=configured):
                f=test_payment_matching.PaymentMatchingTests();f.setUp()
                try:
                    shared='跨企业同名店'
                    if configured:
                        names.save(f.path,names.prepare(names.load(f.path),[{'store':f.target['store'],'warehouse':wh,'value':shared} for wh in ('河北仓','山东仓')]))
                    a=pm.prepare(f.path,[f.document(name=shared)],None,pm.build(f.path),'100')
                    other=next(g for gid in f.f.gids for g in [jst.get(f.path,gid)] if g['head']['warehouse']=='山东仓' and ((g['head']['oid']==f.target['oid']) if configured else g['head']['oid']!=f.target['oid']))
                    b=f.document(other,internal='COMPANY-B',name=shared);b['target']=next(h for h in pm.build(f.path)['heads'] if h['number']==other['head']['number'])
                    lc.commit(f.path,pm.prepare(f.path,[b],None,pm.build(f.path),'200'))
                    self.assertTrue(lc.commit(f.path,a))
                finally:f.tearDown()

    def test_unrelated_payment_commits_do_not_invalidate_target(self):
        import test_payment_matching,payment_matching as pm,jst_orders as jst
        f=test_payment_matching.PaymentMatchingTests();f.setUp()
        try:
            index=pm.build(f.path);plan=pm.prepare(f.path,[f.document()],None,index,None);target=plan['plans'][0]['head']
            other=next(jst.get(f.path,gid) for gid in f.f.gids if jst.get(f.path,gid)['head']['oid']!=target['oid'])
            b=pm.prepare(f.path,[f.document(other,internal='OTHER-B',name='另一门店')],None,pm.build(f.path),None);lc.commit(f.path,b)
            self.assertTrue(lc.commit(f.path,plan))
            with closing(sqlite3.connect(f.path)) as db:self.assertIsNotNone(lc.current(db,target,'payment'))
        finally:f.tearDown()

if __name__=='__main__':unittest.main()
