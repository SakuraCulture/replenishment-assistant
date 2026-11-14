"""Filter explanations reuse active synthetic projections; no real data/platforms."""
import copy
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import daily_batch
import lifecycle_home
import normal_orders as normal
import order_lifecycle as lifecycle
import work_repository as wr
from test_archive_orders import seed as archive_seed


def seed(root,total=13):
    """One warehouse/order; final order retains the original, pre-operations route."""
    work,repo,payload=archive_seed(root)
    with closing(work.connect()) as db,db:
        db.execute('DELETE FROM normal_orders');db.execute('DELETE FROM normal_revisions')
        lifecycle.schema(db)
        for i in range(total):
            oid=f'synthetic-{i:03d}';store=f'S{i:03d}';p=copy.deepcopy(payload)
            r=p['result'];r.update(store=store,store_name='合成门店'+store);r['rows']=r['rows'][:1]
            db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(wr.today(),store,'normal_model',oid,'BH-'+store,1))
            db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',(oid,1,normal.fingerprint(r),wr.now(),normal.encode(p,db)))
            for stage in (('return','operations') if i<total-1 else ('return',)):
                doc={'schema':1,'files':[],'rows':[{'sku':'0001','name':'合成商品','base_quantity':'5'}]}
                db.execute('INSERT INTO lifecycle_versions VALUES(?,?,?,?,?,?,?,?)',(oid,1,'河北仓',stage,1,wr.now(),lifecycle.fingerprint(doc),wr.dumps(doc)))
    return work,repo


class FilterExplanationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.work,self.repo=seed(self.root);self.path=self.work.path;self.cache=self.root/'index.sqlite3'
        self.request={'from':wr.today(),'to':wr.today(),'stage_view':'operations','stage_filter':'本环节全部'}
    def read(self,**kwargs):return daily_batch.read(self.path,self.cache,{**self.request,'explain_filters':True,**kwargs})
    def test_opt_in_keeps_original_result_contract_and_has_no_extra_detail_reads(self):
        # Warm the disposable cache so both requests perform identical source work.
        daily_batch.read(self.path,self.cache,self.request)
        original_get=normal.Repository.get;original_current=lifecycle.current
        with patch.object(normal.Repository,'get',autospec=True,side_effect=original_get) as get,patch.object(lifecycle,'current',side_effect=original_current) as current:
            original=daily_batch.read(self.path,self.cache,self.request);before=(get.call_count,current.call_count)
            explained=self.read();after=(get.call_count-before[0],current.call_count-before[1])
        explanation=explained.pop('filter_explanation')
        self.assertEqual(original,explained)
        self.assertNotIn('filter_explanation',original)
        self.assertEqual(before,after)
        self.assertEqual(explanation['excluded_count'],1)
    def test_13_12_identifies_legacy_order_and_explains_the_actual_stage_filter(self):
        data=self.read();self.assertEqual((data['scope'],data['count'],len(data['records'])),(13,12,12))
        self.assertTrue(all(r['status']=='待付款' for r in data['records']))
        excluded=data['filter_explanation']['excluded'];self.assertEqual(len(excluded),1)
        row=excluded[0]
        self.assertEqual({k:row[k] for k in ('oid','number','store','name','warehouse','status')},
                         {'oid':'synthetic-012','number':'BH-S012-HB','store':'S012','name':'合成门店S012','warehouse':'河北仓','status':'待付款'})
        self.assertEqual(row['reason_codes'],['stage'])
        self.assertIn('旧流程未留存运营签发单据',row['reasons'][0])
        self.assertEqual(set(row),{'oid','number','store','name','warehouse','status','reason_codes','reasons'})
        overview=self.read(stage_view=None)
        self.assertEqual((overview['scope'],overview['count']),(13,13))
        self.assertEqual(overview['filter_explanation'],{'excluded_count':0,'excluded':[]})
    def test_all_compound_exclusion_reasons_are_reported_without_short_circuit(self):
        data=self.read(scope=[['synthetic-000','河北仓']],stage_filter='待处理',warehouse='山东仓',lock='已锁',status='待回传')
        self.assertEqual(data['records'],[]);self.assertEqual(data['scope'],13);self.assertEqual(data['count'],0)
        row=next(r for r in data['filter_explanation']['excluded'] if r['oid']=='synthetic-012')
        self.assertEqual(row['reason_codes'],['scope','stage','warehouse','lock','status'])
        self.assertEqual(len(row['reasons']),5)
        self.assertIn('所选山东仓，该单为河北仓',row['reasons'][2])
        self.assertIn('所选已锁，该单未锁',row['reasons'][3])
        self.assertIn('待回传',row['reasons'][4]);self.assertIn('待付款',row['reasons'][4])
    def test_fixed_scope_does_not_change_total_scope_or_existing_membership(self):
        data=self.read(scope=[['synthetic-000','河北仓']])
        self.assertEqual((data['scope'],data['count']),(13,1));self.assertEqual(data['records'][0]['oid'],'synthetic-000')
        excluded=data['filter_explanation']['excluded'];self.assertEqual(len(excluded),12)
        self.assertEqual(next(r for r in excluded if r['oid']=='synthetic-001')['reason_codes'],['scope'])
        self.assertEqual(next(r for r in excluded if r['oid']=='synthetic-012')['reason_codes'],['scope','stage'])
    def test_explanation_matches_original_filter_membership_across_filter_modes(self):
        requests=[{}, {'stage_view':None}, {'stage_filter':'待处理'},
                  {'warehouse':'山东仓'}, {'lock':'已锁'}, {'lock':'未锁'},
                  {'status':'待付款'}, {'status':'已结束'}, {'status':'全部未完成'},
                  {'stage_view':'return','stage_filter':'本环节全部'},
                  {'stage_view':'payment','stage_filter':'待处理'},
                  {'stage_view':'receipt','stage_filter':'本环节全部'},
                  *[{'stage_view':'jst','stage_filter':s} for s in ('待生成','已生成未锁','已锁','本环节全部')],
                  {'scope':[]}, {'scope':[['synthetic-001','河北仓']]}]
        for overrides in requests:
            with self.subTest(overrides=overrides):
                query={**self.request,**overrides}
                original=daily_batch.read(self.path,self.cache,query)
                explained=daily_batch.read(self.path,self.cache,{**query,'explain_filters':True})
                explanation=explained.pop('filter_explanation')
                self.assertEqual(original,explained)
                self.assertEqual(explanation['excluded_count'],original['scope']-original['count'])
                self.assertEqual(len(explanation['excluded']),explanation['excluded_count'])
                self.assertFalse({r['oid'] for r in original['records']} & {r['oid'] for r in explanation['excluded']})
    def test_pagination_excludes_filter_rejections_only_and_keeps_all_matched_contract(self):
        paged=self.root/'paged';paged.mkdir();work,_=seed(paged,total=63);cache=paged/'cache.sqlite3'
        query={**self.request,'explain_filters':True,'page':1}
        data=daily_batch.read(work.path,cache,query)
        self.assertEqual((data['scope'],data['count'],data['page'],len(data['records'])),(63,62,1,12))
        self.assertEqual([r['list_position'] for r in data['records']],list(range(50,62)))
        self.assertEqual(data['filter_explanation']['excluded_count'],1)
        self.assertEqual(data['filter_explanation']['excluded'][0]['oid'],'synthetic-062')
        all_matched=daily_batch.read(work.path,cache,{**query,'all_matched':True})
        self.assertNotIn('page',all_matched)
        self.assertEqual(len(all_matched['records']),62)
        self.assertEqual(all_matched['filter_explanation'],data['filter_explanation'])
    def test_warm_cache_never_exposes_recycle_bin_or_source_removed_records(self):
        data=self.read();self.assertEqual(data['filter_explanation']['excluded_count'],1)
        with closing(sqlite3.connect(self.cache)) as db:cached=db.execute('SELECT COUNT(*) FROM lifecycle_projection').fetchone()[0]
        with closing(self.work.connect()) as db,db:
            db.execute('CREATE TABLE archive_visibility(oid TEXT,warehouse TEXT,deleted INTEGER,token INTEGER,at TEXT,PRIMARY KEY(oid,warehouse))')
            db.execute('INSERT INTO archive_visibility VALUES(?,?,?,?,?)',('synthetic-012','河北仓',1,1,wr.now()))
        hidden=self.read();self.assertEqual((hidden['scope'],hidden['count']),(12,12))
        self.assertEqual(hidden['filter_explanation'],{'excluded_count':0,'excluded':[]})
        with closing(self.work.connect()) as db,db:
            db.execute('DELETE FROM normal_orders WHERE oid=?',('synthetic-000',))
            db.execute('DELETE FROM normal_revisions WHERE oid=?',('synthetic-000',))
        removed=self.read();self.assertEqual((removed['scope'],removed['count']),(11,11))
        self.assertEqual(removed['filter_explanation'],{'excluded_count':0,'excluded':[]})
        self.assertFalse(any(r['oid'] in ('synthetic-000','synthetic-012') for r in removed['records']))
        with closing(sqlite3.connect(self.cache)) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM lifecycle_projection').fetchone()[0],cached)
    def test_date_and_keyword_scope_exclusions_are_not_misreported_as_filter_rejections(self):
        self.assertEqual(self.read(search='S012')['filter_explanation']['excluded_count'],1)
        self.assertEqual(self.read(search='absent')['filter_explanation'],{'excluded_count':0,'excluded':[]})
        data=self.read(**{'from':'1900-01-01','to':'1900-01-02'})
        self.assertEqual((data['scope'],data['count']),(0,0))
        self.assertEqual(data['filter_explanation'],{'excluded_count':0,'excluded':[]})


@unittest.skipUnless(importlib.util.find_spec('PySide6'),'optional Qt runtime')
class FilterExplanationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        from PySide6.QtWidgets import QWidget
        from lifecycle_home_ui import LifecycleWidget
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.work,_=seed(self.root)
        self.host=QWidget();self.host.work_repository=self.work;self.host.repository=SimpleNamespace(folder=self.root);self.host.closing=False
        self.panel=LifecycleWidget(self.host,'operations')
        self.request={'from':wr.today(),'to':wr.today(),'stage_view':'operations','stage_filter':'本环节全部','explain_filters':True}
        self.data=daily_batch.read(self.work.path,self.root/'cache.sqlite3',self.request)
    def tearDown(self):
        import shiboken6
        self.panel.reader.shutdown();shiboken6.delete(self.host);self.temp.cleanup()
    def test_button_opens_reusable_scrollable_details_with_full_identity_and_reasons(self):
        p=self.panel;self.assertFalse(p.excluded_btn.isEnabled())
        p.query_ready(self.data,self.request)
        self.assertTrue(p.excluded_btn.isEnabled());self.assertEqual(p.excluded_btn.text(),'查看未显示订单（1）')
        self.assertIn('日期/关键词范围 13 单',p.note.text());self.assertIn('符合当前筛选 12 单',p.note.text());self.assertIn('本页显示 12 单',p.note.text())
        with patch('readable_ui.details') as details:
            p.excluded_btn.click();details.assert_called_once()
        args=details.call_args.args
        self.assertEqual(args[1],'未显示订单与原因')
        self.assertEqual(args[2],['补货单号','门店编码','门店名称','仓','当前进度','全部未显示原因'])
        self.assertEqual(args[3][0][:5],['BH-S012-HB','S012','合成门店S012','河北仓','待付款'])
        self.assertIn('旧流程未留存运营签发单据',args[3][0][5])
    def test_no_exclusions_and_failed_or_pending_query_disable_and_clear_entry(self):
        p=self.panel;p.query_ready(self.data,self.request)
        with patch.object(p.reader,'request') as request:
            p.query();self.assertTrue(request.call_args.args[0]['explain_filters'])
        self.assertFalse(p.excluded_btn.isEnabled());self.assertEqual(p.excluded_records,[])
        p.query_ready(self.data,self.request);p.failed('synthetic error')
        self.assertFalse(p.excluded_btn.isEnabled());self.assertEqual(p.excluded_records,[])
        p.query_ready(daily_batch.read(self.work.path,self.root/'cache.sqlite3',{**self.request,'stage_view':None}),self.request)
        self.assertFalse(p.excluded_btn.isEnabled());self.assertEqual(p.excluded_btn.text(),'查看未显示订单（0）')
        with patch('readable_ui.details') as details:p.show_excluded_orders();details.assert_not_called()
    def test_zero_matches_keep_scope_counts_and_explanation_entry(self):
        query={**self.request,'stage_filter':'待处理'}
        self.panel.query_ready(daily_batch.read(self.work.path,self.root/'cache.sqlite3',query),query)
        self.assertIn('日期/关键词范围 13 单',self.panel.note.text());self.assertIn('符合当前筛选 0 单',self.panel.note.text())
        self.assertEqual(self.panel.excluded_btn.text(),'查看未显示订单（13）');self.assertTrue(self.panel.excluded_btn.isEnabled())


if __name__=='__main__':unittest.main()
