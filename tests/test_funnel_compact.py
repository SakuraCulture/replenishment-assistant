"""Explicit new 37-column contract; numerical expectations independent of writer."""
import copy,io,sys,unittest
from pathlib import Path
from decimal import Decimal as D
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).parent)]
import openpyxl
import funnel_compact_export as c
from test_funnel_monitor import fact
from xlsx_exports import stream_book

class Compact(unittest.TestCase):
    def setUp(self):
        self.rows=[fact(),fact(sku='002',unit='瓶',quantities=(20,20,10,10,None),group='非必采'),
                   fact(order='O2',warehouse='河北仓',quantities=(30,30,30,20,0),price='3')]
    def test_thirty_seven_exact_columns_and_all_stage_metrics(self):
        self.assertEqual(len(c.HEADERS),37)
        row=next(c.records(self.rows,'store'))
        self.assertEqual(row[:7],['0001','=测试门店','山东仓 / 河北仓','','2026-10-15','2026-10-15','全部'])
        self.assertEqual(row[7:22],[2,60,150,2,58,146,2,46,122,2,34,88,1,2,4])
        self.assertEqual(row[34:],[D(1),D(2)/40,D(4)/110])
        self.assertEqual(row[31:34],[D(1),D(2)/24,D(4)/68])
    def test_missing_zero_unpriced_and_positive_sku(self):
        rows=[fact(quantities=(10,0,None,None,None),price=None)]
        row=next(c.records(rows,'order'))
        # Existing amount contract: an explicit zero quantity has zero value,
        # even without a price; an unknown quantity never becomes zero.
        self.assertEqual(row[7:13],[1,10,None,0,0,0]);self.assertEqual(row[13:22],[None]*9)
        self.assertEqual(row[22:25],[0,0,None]);self.assertEqual(row[25:],[None]*12)
        self.assertEqual(c.ratios([fact(quantities=(0,5,0,0,0))],'model','signed'),(None,None,None))
    def test_exclusions_groups_order_and_dedup(self):
        rows=self.rows+[fact(order='bad',quantities=(99999,)*5,scope='排除'),fact(order='unknown',scope='待核')]
        self.assertEqual(next(c.records(rows,'store'))[7:],next(c.records(self.rows,'store'))[7:])
        rows=list(c.records(self.rows,'order'))
        self.assertEqual([(r[3],r[6]) for r in rows],[('O1','全部'),('O1','必采'),('O1','非必采'),('O2','全部'),('O2','必采')])
        self.assertEqual(next(c.records(self.rows,'store'))[7],2)
        self.assertEqual(sum(r[7] for r in rows if r[6]=='全部'),3)
    def test_no_rounding_cap_and_ratios_do_not_require_price_for_counts(self):
        rows=[fact(quantities=(10,15,15,15,15),price=None),fact(sku='002',quantities=(10,5,5,5,5),price='2')]
        self.assertEqual(c.ratios(rows,'model','signed'),(D(1),D(1),D('.5')))
        self.assertEqual(c.ratios(rows[:1],'model','signed'),(D(1),D('1.5'),None))
    def test_excel_exact_sheets_types_formats_notes_no_extra_fields(self):
        before=copy.deepcopy(self.rows);buf=io.BytesIO()
        with stream_book() as book:c.write_summary(book,self.rows,'store');book.save(buf)
        b=openpyxl.load_workbook(io.BytesIO(buf.getvalue()))
        try:
            self.assertEqual(b.sheetnames,['门店经营对比','门店分仓对比','订单五段漏斗'])
            for ws in b:
                self.assertEqual(ws.max_column,37);self.assertEqual(list(next(ws.values)),c.HEADERS)
                self.assertEqual(ws.freeze_panes,'H2');self.assertEqual(ws['A2'].value,'0001');self.assertEqual(ws['B2'].data_type,'s')
                self.assertIn('混合',ws['I1'].comment.text);self.assertEqual(ws['W2'].number_format,c.PERCENT)
                self.assertEqual(ws['J2'].number_format,c.MONEY)
                self.assertTrue(all(x.data_type not in ('f','e') for row in ws for x in row))
        finally:b.close()
        self.assertEqual(self.rows,before)

if __name__=='__main__':unittest.main()
