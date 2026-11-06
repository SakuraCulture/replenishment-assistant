"""Independent arithmetic expectations, old projection isolation and API parity."""
import copy, io, sys, threading, unittest
from decimal import Decimal as D
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).parent)]
import openpyxl
import coverage_metrics as c
from test_funnel_monitor import fact
from xlsx_exports import stream_book
from runtime_support import Cancelled


def row(**kw):
    r=fact(**kw);r.update(name='测试商品',observations={'AN':2,'AM':1,'K':4,'L':6,'AG':7,'AH':3})
    return r


class CoverageTests(unittest.TestCase):
    def test_frozen_formula_and_each_stage(self):
        r=row(quantities=(20,20,16,12,10));d=c.item(r)
        self.assertEqual(list(d['stage_days'].values()),list(map(D,[10,10,8,6,5])))
        self.assertEqual((d['available_days'],d['position_days'],d['signed_position_days'],d['protection_days']),(2,5,15,10))
        self.assertEqual(d['signed_to_receipt_loss_days'],5)
        self.assertEqual(len(d['risks']),2)
    def test_no_monthly_substitution(self):
        r=row();r['observations'].update(AN=3,AM=2,J=3000)
        self.assertEqual(c.item(r)['forecast_daily'],6)
    def test_zero_is_not_missing(self):
        r=row(quantities=(10,0,None,None,None));d=c.item(r)
        self.assertEqual(d['stage_days']['signed'],0);self.assertIsNone(d['stage_days']['return'])
        self.assertTrue(d['stage_reasons']['return'])
    def test_no_forecast_missing_invalid_and_new(self):
        for value in (None,0,-1,'NaN','Infinity','',True):
            r=row();r['observations']['AN']=value
            with self.subTest(value=value):
                d=c.item(r);self.assertTrue(d['base_reasons']);self.assertIsNone(d['stage_days']['model'])
        r=row();r.pop('observations');self.assertIn('历史预测',c.item(r)['base_reasons'][0])
    def test_price_does_not_block_sku_but_blocks_weighting(self):
        for price in (None,'0','-2','NaN'):
            r=row(price=price)
            self.assertEqual(c.item(r)['stage_days']['model'],5)
            self.assertIsNone(c.summarize([r])['stage_days']['model'])
    def test_stock_negative_not_clamped_and_unit_required(self):
        r=row();r['observations']['K']=-1
        self.assertIsNone(c.item(r)['available_days']);self.assertEqual(c.item(r)['stage_days']['model'],5)
        r['unit']='';self.assertIsNone(c.item(r)['stage_days']['model'])
    def test_weighted_different_units_and_zero_keeps_denominator(self):
        a=row(price='2');b=row(sku='002',unit='瓶',quantities=(30,0,0,0,0),price='4');b['observations']['AN']=10
        result=c.summarize([a,b]);self.assertEqual(result['daily_value'],44)
        self.assertEqual(result['stage_days']['model'],D(140)/44)
        self.assertEqual(result['stage_days']['signed'],D(16)/44)
    def test_common_orders_and_incomplete_order_not_partial(self):
        a=row();b=row(order='O2',sku='002',quantities=(10,8,None,None,None))
        r=c.summarize([a,b]);self.assertIsNone(r['stage_days']['return'])
        r=c.summarize([a,b],through='return');self.assertEqual(r['orders'],1);self.assertEqual(r['excluded_order_count'],1)
        self.assertEqual([r['stage_days'][s] for s in ('model','signed','return')],[5,4,3]);self.assertIsNone(r['stage_days']['payment'])
    def test_cross_warehouse_denominator_once(self):
        a=row();b=row(order='O2',warehouse='河北仓')
        r=c.summarize([a,b]);self.assertEqual(r['unique_demand_items'],1)
        self.assertEqual(r['daily_value'],4);self.assertEqual(r['stage_days']['model'],10)
    def test_cross_warehouse_conflict_not_guessed(self):
        for field,value in [('AN',3),('K',None),('L',7)]:
            a=row();b=row(order='O2',warehouse='河北仓');b['observations'][field]=value
            r=c.summarize([a,b]);self.assertTrue(r['conflicts']);self.assertIsNone(r['stage_days']['model'])
    def test_cross_round_latest_only(self):
        old=row(order='OLD',day='2026-10-01',quantities=(999,)*5);new=row()
        r=c.projection([old,new]);self.assertEqual(r['stores'][0]['coverage']['stage_days']['model'],5)
        self.assertEqual(r['stores'][0]['coverage']['basis_day'],'2026-10-15');self.assertEqual(len(r['rounds']),2)
    def test_exclusions_mandatory_groups_and_no_mutation(self):
        rows=[row(),row(sku='002',scope='排除'),row(sku='003',scope='待核'),row(sku='004',group='非必采')];before=copy.deepcopy(rows)
        r=c.projection(rows);self.assertEqual(r['overview']['eligible_rows'],2)
        self.assertEqual(r['groups']['必采']['eligible_rows'],1);self.assertEqual(rows,before)
    def test_duplicate_records_reject_and_cancel(self):
        with self.assertRaises(ValueError):c.projection([row(),row()])
        stop=threading.Event();stop.set()
        with self.assertRaises(Cancelled):c.projection([row()],cancel=stop)
    def test_extended_excel_keeps_first37_cells_and_styles(self):
        from funnel_compact_export import write_summary,HEADERS
        rows=[row(),row(sku='002',quantities=(20,0,None,None,None))];books=[]
        for expanded in (False,True):
            buf=io.BytesIO()
            with stream_book() as book:write_summary(book,rows,'store',coverage=expanded);book.save(buf)
            books.append(openpyxl.load_workbook(io.BytesIO(buf.getvalue())))
        try:
            self.assertEqual(books[0].sheetnames,books[1].sheetnames)
            for name in books[0].sheetnames:
                aa,bb=books[0][name],books[1][name];self.assertEqual(bb.max_column,42)
                for ra,rb in zip(aa,bb):
                    self.assertEqual([(x.value,x.data_type,x.number_format,copy.copy(x.font),copy.copy(x.fill),copy.copy(x.alignment)) for x in ra],[(x.value,x.data_type,x.number_format,copy.copy(x.font),copy.copy(x.fill),copy.copy(x.alignment)) for x in rb[:37]])
                self.assertEqual(bb['AL2'].data_type,'n');self.assertEqual(bb['AL2'].number_format,'0.0')
                self.assertTrue(bb['AN2'].comment);self.assertIsNone(bb['AN2'].value)
        finally:
            for b in books:b.close()


if __name__=='__main__':unittest.main()
