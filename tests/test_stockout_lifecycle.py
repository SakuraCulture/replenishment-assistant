"""Isolated status filters and recoverable analysis lifecycle; no native platform calls."""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from test_stockout_analysis import workbook
from stockout_repository import Repository
from stockout_facts import load_catalog,load_store
from stockout_common import encoded,file_hash
from stockout_lifecycle import preview,recalculate,delete_log,rerun
from runtime_support import Cancelled
import stockout_metrics as metrics


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.repo=Repository(self.root);self.repo.initialize()
        self.settings={'stores':[{'code':'S','name':'测试门店S'},{'code':'T','name':'测试门店T'}],
                       'routes':{'S':{'primary':'山东仓','secondary':None},'T':{'primary':'河北仓','secondary':None}},'profile':None}
        self.repo.save_settings(self.settings)

    def seed(self,*,partial=False,day='2026-11-07',negative=False):
        catalog={}
        for warehouse in ('山东仓','河北仓'):
            path=workbook(self.root/(warehouse+'.xlsx'),['商品编码','商品名称','商品状态','可用数','分类','供应商名称','虚拟分类'],
                          [['A','商品A','启用',10,warehouse+'类目',warehouse+'供应商','']])
            catalog[warehouse]=load_catalog(self.repo,path,warehouse)
        run=self.repo.new_run(day=day);self.repo.change(run['id'],catalogs=catalog)
        for store in ('S',) if partial else ('S','T'):
            path=workbook(self.root/(store+'.xlsx'),['仓库/门店编码','商品编码','商品名称','月销','可用库存','补货状态'],
                          [[store,'A','商品A',2,(-3 if negative else 0) if store=='S' else 1,'正常']])
            load_store(self.repo,run['id'],store,path,observed=day+'T08:00:00+08:00')
        return self.repo.publish(run['id'])

    def test_legacy_negative_snapshot_recalculates_offline_and_preserves_old_revision(self):
        old=self.seed(negative=True);settings=self.repo.settings()
        # Persist the v1 derived state over a real negative-stock original.
        # This fixture models a database produced before the normalization rule.
        with self.repo.connect(write=True) as db:
            sid=db.execute("SELECT snapshot FROM run_stores WHERE run=? AND store='S'",(old['id'],)).fetchone()[0]
            db.execute('UPDATE fact_records SET stock=-3,bad=1,zero=0 WHERE snapshot=?',(sid,))
            db.execute('UPDATE dimension_summaries SET bad=1,zero=0 WHERE snapshot=?',(sid,))
            db.execute('UPDATE summaries SET bad=1,zero=0,unknown_zero=0 WHERE snapshot=?',(sid,))
            original=tuple(db.execute('SELECT * FROM facts WHERE snapshot=?',(sid,)).fetchone())
            files=[(r['sha'],r['path']) for r in db.execute('SELECT * FROM originals')]
        q={'start':'2026-11-07'}
        before=metrics.query(self.repo,q)
        self.assertIsNone(before['total']['rate']);self.assertTrue(before['needs_negative_stock_recalculation'])
        self.assertFalse(metrics.query(self.repo,{**q,'filters':{'store':['T']}})['needs_negative_stock_recalculation'])
        from stockout_exports import export
        import openpyxl
        path=self.root/'legacy.xlsx';export(self.repo,q,path)
        book=openpyxl.load_workbook(path,read_only=True)
        try:self.assertIn('需用留存原件重算',{row[0]:row[1] for row in book['口径与覆盖'].values}['统计口径版本'])
        finally:book.close()
        with patch('stockout_acquisition.execute',side_effect=AssertionError('No platform acquisition during recalculation')):
            new=recalculate(self.repo,preview(self.repo,old['id'],'recalculate','original'))
        self.assertNotEqual(new['id'],old['id'])
        after=metrics.query(self.repo,q)
        self.assertEqual((after['total']['base'],after['total']['zero'],after['total']['bad'],after['total']['rate']),(2,1,0,.5))
        self.assertFalse(after['needs_negative_stock_recalculation'])
        row=next(r for r in metrics.detail(self.repo,q)['items'] if r['store']=='S')
        self.assertEqual((row['stock_raw'],row['stock'],row['zero'],row['bad']),('-3',0,1,0))
        with self.repo.connect() as db:
            self.assertEqual(tuple(db.execute('SELECT * FROM facts WHERE snapshot=?',(sid,)).fetchone()),original)
        self.assertEqual(self.repo.settings(),settings)
        self.assertTrue(all(file_hash(Path(p))==h for h,p in files))

    def test_original_recalc_creates_revision_same_data_and_never_updates_current_settings(self):
        old=self.seed();before=metrics.detail(self.repo,{'start':'2026-11-07'})
        self.repo.save_settings({**self.settings,'routes':{'S':{'primary':'河北仓','secondary':None},'T':self.settings['routes']['T']}})
        current=self.repo.settings();plan=preview(self.repo,old['id'],'recalculate','original')
        new=recalculate(self.repo,plan)
        self.assertEqual(self.repo.settings(),current);self.assertEqual(new['state'],'COMPLETE')
        after=metrics.detail(self.repo,{'start':'2026-11-07'})
        keys=('store','sku','source_sha','observed','day','month','stock','source','warehouse','category','supplier')
        self.assertEqual([[r[k] for k in keys] for r in before['items']],[[r[k] for k in keys] for r in after['items']])
        self.assertNotEqual(new['stores'][0]['snapshot'],old['stores'][0]['snapshot'])
        with self.repo.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM store_snapshots').fetchone()[0],4)
            self.assertEqual(db.execute('SELECT count(*) FROM publications').fetchone()[0],2)
        self.assertEqual(self.repo.run(old['id']),old)

    def test_current_recalc_uses_new_route_but_original_stock_date_and_catalogs(self):
        old=self.seed()
        current=self.repo.save_settings({**self.settings,'routes':{'S':{'primary':'河北仓','secondary':None},'T':self.settings['routes']['T']}})
        new=recalculate(self.repo,preview(self.repo,old['id'],'recalculate'))
        rows=metrics.detail(self.repo,{'start':'2026-11-07'})['items']
        s=next(r for r in rows if r['store']=='S')
        self.assertEqual((s['warehouse'],s['category'],s['stock'],s['day']),('河北仓','河北仓类目',0,'2026-11-07'))
        self.assertEqual(self.repo.settings(),current);self.assertEqual(new['source_run'],old['id'])

    def test_recalc_preserves_partial_coverage_and_missing_stores(self):
        old=self.seed(partial=True);new=recalculate(self.repo,preview(self.repo,old['id'],'recalculate','original'))
        self.assertEqual(new['state'],'PARTIAL')
        coverage=metrics.query(self.repo,{'start':'2026-11-07'})['coverage'][0]
        self.assertEqual((coverage['selected'],coverage['completed']),(2,1))

    def test_recalc_missing_current_scope_or_historical_catalog_is_explicit(self):
        old=self.seed()
        self.repo.save_settings({**self.settings,'stores':self.settings['stores'][:1]})
        with self.assertRaisesRegex(ValueError,'全部门店'):preview(self.repo,old['id'],'recalculate')
        self.repo.save_settings(self.settings)
        with self.repo.connect(write=True) as db:
            db.execute("UPDATE store_snapshots SET catalog_set=? WHERE store='S'",(encoded({'河北仓':old['catalogs']['河北仓']}),))
        with self.assertRaisesRegex(ValueError,'历史仓原件'):preview(self.repo,old['id'],'recalculate')

    def test_recalc_rejects_superseded_source_and_changed_settings_preview(self):
        old=self.seed();plan=preview(self.repo,old['id'],'recalculate')
        self.repo.save_settings({**self.settings,'note':'changed'})
        with self.assertRaisesRegex(ValueError,'变化'):recalculate(self.repo,plan)
        self.seed()
        with self.assertRaisesRegex(ValueError,'当前日报'):preview(self.repo,old['id'],'recalculate','original')

    def test_recalc_corrupt_original_does_not_replace_published_data(self):
        old=self.seed();plan=preview(self.repo,old['id'],'recalculate','original')
        old_result=metrics.query(self.repo,{'start':'2026-11-07'})
        Path(plan['snapshots'][0]['path']).write_bytes(b'corrupt synthetic original')
        with self.assertRaisesRegex(ValueError,'校验值'):recalculate(self.repo,plan)
        self.assertEqual(metrics.query(self.repo,{'start':'2026-11-07'}),old_result)
        self.assertEqual(self.repo.recent()[0]['state'],'REVIEW')

    def test_recalc_cancellation_keeps_old_publication_and_resumes_without_rpc(self):
        old=self.seed();plan=preview(self.repo,old['id'],'recalculate','original');cancel=threading.Event()
        def progress(message):
            if message.startswith('重算 '):cancel.set()
        with self.assertRaises(Cancelled):recalculate(self.repo,plan,cancel=cancel,progress=progress)
        paused=self.repo.recent()[0];self.assertEqual(paused['state'],'PAUSED')
        with self.repo.connect() as db:self.assertEqual(db.execute('SELECT run FROM publications p JOIN current_days d ON d.publication=p.id').fetchone()[0],old['id'])
        result=recalculate(self.repo,resume=paused['id']);self.assertEqual(result['state'],'COMPLETE')

    def test_recalc_publication_compare_and_swap_protects_later_results(self):
        old=self.seed();plan=preview(self.repo,old['id'],'recalculate','original')
        def change(message):
            if message.startswith('正在提交'):
                with self.repo.connect(write=True) as db:
                    db.execute('DELETE FROM current_days')
        with self.assertRaisesRegex(ValueError,'日报已变化'):recalculate(self.repo,plan,progress=change)
        with self.repo.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM publications').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM current_days').fetchone()[0],0)

    def test_log_removal_keeps_results_originals_and_native_identity(self):
        old=self.seed()
        with self.repo.connect(write=True) as db:
            db.execute('UPDATE runs SET external=?,message=? WHERE id=?',(encoded({'ax_commands':{'S':'native-1'},'identity':{'account':'frozen'},'versions':{'version':'test'},'verbose':'remove me'}),'old verbose',old['id']))
            db.execute("UPDATE run_stores SET error='old detail' WHERE run=?",(old['id'],))
        before=metrics.query(self.repo,{'start':'2026-11-07'});files={p.name:file_hash(p) for p in (self.repo.folder/'originals').iterdir()}
        delete_log(self.repo,preview(self.repo,old['id'],'delete_log'))
        self.assertEqual(self.repo.recent(),[]);self.assertEqual(metrics.query(self.repo,{'start':'2026-11-07'}),before)
        run=self.repo.run(old['id']);self.assertEqual(run['external']['ax_commands'],{'S':'native-1'})
        self.assertNotIn('verbose',run['external']);self.assertTrue(all(s['error']=='' for s in run['stores']))
        self.assertEqual(files,{p.name:file_hash(p) for p in (self.repo.folder/'originals').iterdir()})
        self.assertEqual(self.repo.recent(include_result_links=True)[0]['id'],old['id'])
        self.assertEqual(preview(self.repo,old['id'],'recalculate','original')['run'],old['id'])

    def test_log_removal_cancel_stale_and_uncertain_task_are_guarded(self):
        old=self.seed(partial=True);plan=preview(self.repo,old['id'],'delete_log');cancel=threading.Event();cancel.set()
        with self.assertRaises(Cancelled):delete_log(self.repo,plan,cancel)
        with self.repo.connect(write=True) as db:db.execute('UPDATE runs SET external=? WHERE id=?',(encoded({'ax_commands':{'S':'uncertain'}}),old['id']))
        with self.assertRaisesRegex(ValueError,'核对平台'):preview(self.repo,old['id'],'delete_log')
        with self.assertRaises(ValueError):delete_log(self.repo,plan)
        self.assertEqual(len(self.repo.recent()),1)

    def test_data_deletion_and_logs_are_independent_and_deleted_data_cannot_recalc(self):
        from stockout_maintenance import preview as clean_plan,execute
        old=self.seed();source_hashes={p.name:file_hash(p) for p in self.root.glob('*.xlsx')}
        execute(self.repo,clean_plan(self.repo,'2026-11-07','2026-11-07'))
        self.assertEqual(metrics.query(self.repo,{'start':'2026-11-07'})['total']['base'],0)
        self.assertEqual(len(self.repo.recent()),1)
        with self.assertRaisesRegex(ValueError,'被删除'):preview(self.repo,old['id'],'recalculate','original')
        delete_log(self.repo,preview(self.repo,old['id'],'delete_log'));self.assertEqual(self.repo.recent(),[])
        self.assertEqual(source_hashes,{p.name:file_hash(p) for p in self.root.glob('*.xlsx')})

    def test_rerun_makes_fresh_intention_retains_scope_and_uses_today(self):
        old=self.seed();plan=preview(self.repo,old['id'],'rerun')
        self.repo.save_settings({**self.settings,'stores':self.settings['stores'][:1]})
        observed=[]
        def capture(repo,rid,rpc,cancel,progress):
            observed.append(repo.run(rid));return observed[-1]
        with patch('stockout_acquisition.execute',side_effect=capture):new=rerun(self.repo,plan,lambda *_:self.fail('no direct rpc'))
        self.assertEqual(new['basis'],old['basis']);self.assertEqual(len(new['stores']),2)
        self.assertEqual(new['day'],plan['day']);self.assertEqual(new['catalogs'],{})
        self.assertNotIn('ax_commands',new['external']);self.assertNotIn('recalculate',new['external'])
        self.assertEqual(self.repo.run(old['id']),old)

    def test_rerun_and_log_deletion_refuse_active_task(self):
        old=self.seed();self.repo.new_run()
        for action in ('rerun','delete_log','recalculate'):
            with self.subTest(action=action),self.assertRaisesRegex(ValueError,'未完成'):preview(self.repo,old['id'],action)

    def test_rerun_refuses_to_replace_other_stores_current_day(self):
        from stockout_common import today
        self.repo.save_settings({**self.settings,'stores':self.settings['stores'][:1]})
        small=self.seed(partial=True,day=today())
        self.repo.save_settings(self.settings);self.seed(day=today())
        with self.assertRaisesRegex(ValueError,'其他门店'):preview(self.repo,small['id'],'rerun')

    def test_partial_end_checks_platform_state_and_preserves_published_data(self):
        from stockout_acquisition import finish_local
        old=self.seed(partial=True)
        with self.repo.connect(write=True) as db:
            db.execute('UPDATE runs SET external=? WHERE id=?',(encoded({'catalog_operation':'op','catalogs_pending':True}),old['id']))
        total=metrics.query(self.repo,{'start':'2026-11-07'})
        with self.assertRaisesRegex(ValueError,'尚未完成'):
            finish_local(self.repo,old['id'],lambda *a,**k:{'batches':[]},None)
        ended=finish_local(self.repo,old['id'],lambda *a,**k:{'batches':[{'operation_id':'op','items':[{'adoption':{'purpose':'stockout_analysis'}}]}]},None)
        self.assertEqual(ended['state'],'ENDED');self.assertFalse(ended['external']['catalogs_pending'])
        self.assertEqual(metrics.query(self.repo,{'start':'2026-11-07'}),total)
        delete_log(self.repo,preview(self.repo,old['id'],'delete_log'));self.assertEqual(self.repo.recent(),[])


class StatusFilterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.repo=Repository(self.root);self.repo.initialize()
        self.repo.save_settings({'stores':[{'code':'S','name':'门店S'}],'routes':{'S':{'primary':'山东仓','secondary':None}},'profile':None})
        path=workbook(self.root/'catalog.xlsx',['商品编码','商品状态','可用数','虚拟分类'],
                      [['A','启用',1,''],['B','禁用',1,''],['C','禁用',1,'标杆淘汰;淘便考核'],['D','备用',1,''],['E','启用',1,'']])
        sha=load_catalog(self.repo,path,'山东仓');rid=self.repo.new_run()['id'];self.repo.change(rid,catalogs={'山东仓':sha})
        self.values=[('A',1,0,'停采'),('B',1,1,'正常'),('C',1,0,'禁采'),('D',1,0,'正常'),('E',1,1,'正常'),('F',1,0,'正常'),('G',1,0,None),('H',None,-1,'停采')]
        path=workbook(self.root/'store.xlsx',['仓库/门店编码','商品编码','月销','可用库存','补货状态'],[['S',*v] for v in self.values])
        load_store(self.repo,rid,'S',path,observed='2026-11-07T08:00:00+08:00');self.repo.publish(rid)
        self.q={'start':'2026-11-07','exclude_stopped':True,'exclude_disabled':True}

    def test_combined_status_filters_remove_both_sides_once_keep_unknown_and_standby(self):
        full=metrics.query(self.repo,{'start':'2026-11-07'})['total'];self.assertEqual((full['base'],full['zero'],full['bad']),(7,5,1))
        for view in ('all','retired','assessment','both'):
            q={**self.q,'view':view};v=metrics.query(self.repo,q)['total']
            self.assertEqual((v['base'],v['zero'],v['bad'],v['removed_base'],v['removed_zero']),(4,3,0,3,2))
            self.assertEqual(v['rate'],.75)
            self.assertEqual({r['sku'] for r in metrics.detail(self.repo,q)['items']},{'D','E','F','G'})
            self.assertEqual({r['sku'] for r in metrics.detail(self.repo,q,excluded=True)['items']},{'A','B','C','H'})

    def test_status_switches_independent_and_aggregate_raw_trend_cut_equal(self):
        for stop,disable,expected in [(False,False,(7,5)),(True,False,(5,3)),(False,True,(5,4)),(True,True,(4,3))]:
            q={**self.q,'exclude_stopped':stop,'exclude_disabled':disable}
            total=metrics.query(self.repo,q)['total'];self.assertEqual((total['base'],total['zero']),expected)
            cuts=metrics.cuts(self.repo,q,['source','product_status'])
            self.assertEqual(sum(r['base'] for r in cuts['items']),total['base'])
            with patch.object(metrics,'fact_source',return_value='facts'):self.assertEqual(cuts,metrics.cuts(self.repo,q,['source','product_status']))
            self.assertEqual(metrics.trend(self.repo,q)[0]['total'],total)

    def test_status_filtered_export_matches_detail_removed_flags_and_context(self):
        from stockout_exports import export
        import openpyxl
        path=self.root/'export.xlsx';export(self.repo,self.q,path,include_detail=True)
        book=openpyxl.load_workbook(path,read_only=True)
        try:
            self.assertEqual(list(book['总体'].values)[1][:3],(4,3,.75))
            rows=list(book['商品明细1'].values);heads=rows[0];i=heads.index('本视角剔除');sku=heads.index('商品编码')
            self.assertEqual({r[sku] for r in rows[1:] if r[i]},{'A','B','C','H'})
            context=dict(list(book['口径与覆盖'].values)[:11]);self.assertIn('禁用',context['视角']);self.assertIn('停采',context['状态剔除'])
        finally:book.close()

    def test_invalid_switch_never_silently_excludes(self):
        with self.assertRaisesRegex(ValueError,'勾选'):metrics.query(self.repo,{**self.q,'exclude_stopped':'false'})

    def test_disabled_uses_selected_supply_warehouse_not_other_warehouse(self):
        settings=self.repo.settings();settings['routes']['S']['secondary']='河北仓';self.repo.save_settings(settings)
        path=workbook(self.root/'secondary.xlsx',['商品编码','商品状态','可用数'],[['A','禁用',1],['B','启用',1],['C','启用',1]])
        hb=load_catalog(self.repo,path,'河北仓');old=self.repo.run(self.repo.recent()[0]['id'])
        new=self.repo.new_run();self.repo.change(new['id'],catalogs={**old['catalogs'],'河北仓':hb})
        load_store(self.repo,new['id'],'S',self.root/'store.xlsx',observed='2026-11-07T09:00:00+08:00');self.repo.publish(new['id'])
        rows=metrics.detail(self.repo,{**self.q,'exclude_stopped':False})['items']
        by={r['sku']:r for r in rows}
        self.assertEqual((by['A']['warehouse'],by['A']['product_status']),('山东仓','启用'))
        self.assertEqual((by['B']['warehouse'],by['B']['product_status']),('河北仓','启用'))
        self.assertIn('C',by)


if __name__=='__main__':unittest.main()
