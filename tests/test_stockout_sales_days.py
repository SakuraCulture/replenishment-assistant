"""Same requested per-SKU formula in query, UI projection and Excel export."""
import sys,unittest
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).parent)]
import stockout_metrics as m
import test_stockout_analysis as fixtures


class SalesDaysTests(unittest.TestCase):
    def test_formula_keeps_zero_negative_and_fractional_values(self):
        for stock,inbound,month,expected in [(10,5,30,15),(0,0,10,0),(-2,1,30,-1),(.5,1.5,4,15)]:
            self.assertEqual(m.expected_sales_days(dict(stock=stock,inbound=inbound,month=month))['expected_sales_days'],expected)
    def test_zero_month_and_missing_inbound_are_not_zero_days(self):
        for row in [dict(stock=1,inbound=2,month=0),dict(stock=1,inbound=None,month=3),dict(stock=None,inbound=2,month=3),dict(stock=1,inbound=2,month=None)]:
            result=m.expected_sales_days(row)
            self.assertIsNone(result['expected_sales_days']);self.assertTrue(result['expected_sales_days_reason'])
    def test_detail_and_export_use_same_snapshot_without_changing_rate(self):
        fixture=fixtures.AnalysisTests();fixture.setUp()
        try:
            fixture.publish([('normal',30,9),('zero',0,1),('negative',30,-7)])
            query={'start':'2026-11-07','view':'all'};before=m.query(fixture.repo,query)
            rows={r['sku']:r for r in m.detail(fixture.repo,query)['items']}
            self.assertEqual(rows['normal']['expected_sales_days'],15)
            self.assertEqual(rows['negative']['expected_sales_days'],-1)
            self.assertIsNone(rows['zero']['expected_sales_days'])
            self.assertEqual(m.query(fixture.repo,query),before)
            from stockout_exports import export
            import openpyxl
            path=fixture.root/'days.xlsx';export(fixture.repo,query,path,include_detail=True)
            book=openpyxl.load_workbook(path,read_only=True,data_only=True)
            try:
                values=list(book['商品明细1'].values);headers=values[0]
                result={r[headers.index('商品编码')]:r[headers.index('预计销售天数')] for r in values[1:]}
                self.assertEqual(result,{'normal':15,'negative':-1,'zero':None})
            finally:book.close()
        finally:fixture.doCleanups()
