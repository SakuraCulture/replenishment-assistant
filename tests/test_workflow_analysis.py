"""Independent cohorts, historical cutoffs and clean report evidence."""
from contextlib import closing
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch
import copy,sqlite3,unittest,openpyxl
import workflow_analysis as w
import order_analytics as analytics,order_analytics_exports as exports
import test_order_analytics as fixture
import jst_orders as j,jst_batches as batches
from test_jst import catalog

def row(day='2026-11-09',sku='A',before=100,after=50):
    return dict(day=day,sku=sku,store='T',warehouse='山东仓',order=day+'-T',unit='个',scope='纳入',price=D('2'),
                quantities={s:D(str(before if s in ('model','signed') else after)) for s in w.STAGES},
                valid={s:True for s in w.STAGES})

class Metrics(unittest.TestCase):
    def test_weighted_week_is_not_mean_of_daily_rates(self):
        rows=[row(before=100,after=50),row(day='2026-11-10',before=10,after=10)]
        p=w.trends(rows,'2026-11-09','2026-11-15','week','quantity')[0]
        self.assertEqual(p['pairs'][1]['rate'],D(60)/110);self.assertNotEqual(p['pairs'][1]['rate'],D('.75'))
        self.assertFalse(p['partial']);self.assertEqual(p['pairs'][1]['orders'],2)
    def test_sku_union_over_period_not_sum_of_days(self):
        rows=[row(),row(day='2026-11-10')];self.assertEqual(w.summary(rows,'skus')['stages']['return'],1)
    def test_missing_day_has_gap_and_no_cross_gap_change(self):
        p=w.trends([row(),row(day='2026-11-11')],'2026-11-09','2026-11-11','day','value')
        self.assertIsNone(p[1]['pairs'][1]['rate']);self.assertIsNone(p[2]['delta_pp'][1])
    def test_zero_and_unknown_remain_distinct(self):
        r=row(after=0);self.assertEqual(w.pair([r],'signed','return','quantity')['rate'],0)
        r['valid']['return']=False;self.assertIsNone(w.pair([r],'signed','return','quantity')['rate'])
        r=row(before=0,after=1);self.assertIsNone(w.pair([r],'signed','return','quantity')['rate'])
    def test_original_ratios_unchanged_by_operations_and_allocation(self):
        r=row();before=w.summary([r]);r['quantities']['operations']=D(200);r['quantities']['allocation']=D(7)
        self.assertEqual(before['pairs'][:5],w.summary([r])['pairs'][:5])
    def test_partial_period_and_more_than_100_percent(self):
        p=w.trends([row(day='2026-11-10',before=5,after=10)],'2026-11-10','2026-11-10','week','quantity')[0]
        self.assertTrue(p['partial']);self.assertEqual(p['pairs'][1]['rate'],2)
    def test_missing_price_does_not_turn_into_free_goods(self):
        r=row();r['price']=None;self.assertIsNone(w.metric([r],'return','value'))
        self.assertEqual(w.metric([r],'return','quantity'),50)

class Projections(unittest.TestCase):
    def setUp(self):
        self.f=fixture.AnalyticsTests();self.f.setUp();self.path=self.f.path
        self.backup=patch('storage_chunks.backup_before_write');self.backup.start()
    def tearDown(self):self.backup.stop();self.f.tearDown()
    def test_operations_and_saved_allocation_are_distinct_facts(self):
        self.f.f.save('return',[{'门店确认数量':40}]);self.f.f.save('operations',[{'门店确认数量':30}])
        done=batches.execute(self.path,batches.preview_new(self.path,[self.f.f.h],{'河北仓':catalog(stock=17)}))
        self.assertFalse(done['errors']);report=self.f.report();r=report['rows'][0]
        self.assertEqual(r['quantities']['return'],40);self.assertEqual(r['quantities']['operations'],30)
        self.assertEqual(r['quantities']['allocation'],17);self.assertTrue(r['valid']['allocation'])
        before=report['snapshot_id'];j.prepare_refresh(self.path,'河北仓',catalog=catalog(stock=999))
        self.assertEqual(self.f.report()['snapshot_id'],before)
    def test_zero_allocation_is_a_known_execution_fact(self):
        self.f.f.save('return',[{'门店确认数量':10}])
        done=batches.execute(self.path,batches.preview_new(self.path,[self.f.f.h],{'河北仓':catalog(stock=0)}))
        self.assertEqual(len(done['zero']),1);r=self.f.report()['rows'][0]
        self.assertEqual(r['quantities']['allocation'],0);self.assertTrue(r['valid']['allocation'])
    def test_allocation_journal_identities_are_scanned_once_per_query(self):
        self.f.f.save('return',[{'门店确认数量':10}])
        batches.execute(self.path,batches.preview_new(self.path,[self.f.f.h],{'河北仓':catalog(stock=10)}))
        real=sqlite3.connect;statements=[]
        def connect(*args,**kwargs):
            db=real(*args,**kwargs);db.set_trace_callback(statements.append);return db
        with patch('sqlite3.connect',side_effect=connect):self.f.report()
        self.assertEqual(sum('FROM jst_events e JOIN normal_orders' in sql for sql in statements),1)
        self.assertFalse(any('FROM jst_events WHERE oid=' in sql for sql in statements))
    def test_historical_cutoff_selects_old_versions_not_future_correction(self):
        self.f.f.save('return',[{'门店确认数量':40}]);self.f.f.save('operations',[{'门店确认数量':10}]);self.f.f.save('payment',[{'付款单商品数量':7}]);self.f.f.save('operations',[{'门店确认数量':30}])
        day=self.f.spec['from']
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('UPDATE normal_revisions SET saved=?',(day+'T09:00:00+08:00',))
            db.execute("UPDATE lifecycle_versions SET saved=? WHERE stage='return'",(day+'T10:00:00+08:00',))
            db.execute("UPDATE lifecycle_versions SET saved=? WHERE stage='operations' AND version=1",(day+'T11:00:00+08:00',))
            db.execute("UPDATE lifecycle_versions SET saved=? WHERE stage='payment'",(day+'T12:00:00+08:00',))
            db.execute("UPDATE lifecycle_versions SET saved=? WHERE stage='operations' AND version=2",(day+'T13:00:00+08:00',))
        old=self.f.report(as_of=day+'T12:30:00+08:00')['rows'][0];current=self.f.report()['rows'][0]
        self.assertEqual(old['quantities']['operations'],10);self.assertTrue(old['valid']['payment']);self.assertEqual(old['quantities']['payment'],7)
        self.assertEqual(current['quantities']['operations'],30);self.assertFalse(current['valid']['payment'])
    def test_clean_export_has_business_first_ids_last_and_native_chart(self):
        self.f.f.save('return');self.f.f.save('operations');report=self.f.report();target=self.f.root/'workflow.xlsx'
        before=self.path.read_bytes();receipt=exports.export(self.path,report,target)
        book=openpyxl.load_workbook(target)
        try:
            self.assertEqual(book.sheetnames,receipt['sheets']);self.assertEqual(book.sheetnames[0],'漏斗总览')
            for name in ['经营主表','商品款数','签发价货值']:
                heads=next(book[name].values);self.assertEqual(heads[:3],('业务日期','门店编码','门店名称'))
                self.assertEqual(heads[-3:],('签发单号','聚水潭内部订单号','翱象采购单号'))
                self.assertNotIn('供应商资料状态',heads);self.assertIn('运营签发',heads);self.assertIn('聚水潭分配',heads)
            self.assertEqual(len(book['原五段比率看板']._charts),1)
            names={r[4] for r in list(book['漏斗率日趋势'].values)[1:]};self.assertEqual(names,set(w.RATIOS))
        finally:book.close()
        self.assertEqual(self.path.read_bytes(),before)
    def test_linkage_missing_databases_creates_nothing(self):
        import workflow_stockout
        out=workflow_stockout.read(self.path,self.f.spec);self.assertFalse(out['store']);self.assertFalse(out['warehouse'])
        self.assertEqual(len(out['notes']),2);self.assertFalse((self.path.parent/'analysis').exists())
    def test_as_of_without_timezone_is_not_silently_localized(self):
        with self.assertRaisesRegex(ValueError,'时区'):self.f.report(as_of=self.f.spec['from']+'T12:00:00')
