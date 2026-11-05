"""Monitoring layout and denominator regression, using isolated synthetic facts."""
import copy
from decimal import Decimal as D
import io
from pathlib import Path
import sys
import threading
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).parent)]
import openpyxl
import funnel_monitor_export as m
from runtime_support import Cancelled
from xlsx_exports import stream_book


def fact(order='O1', sku='001', unit='包', quantities=(10,8,6,4,2), price='2', group='必采', warehouse='山东仓', scope='纳入', day='2026-10-15'):
    return dict(order=order,sku=sku,unit=unit,quantities=dict(zip(m.service.STAGES,[None if x is None else D(x) for x in quantities])),
                valid=dict(zip(m.service.STAGES,[x is not None for x in quantities])),price=None if price is None else D(price),
                group=group,warehouse=warehouse,scope=scope,day=day,store='0001',store_name='=测试门店',
                payment_currency='CNY',payment_amount=D(7))


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.rows = [fact(),fact(sku='002',unit='瓶',quantities=(20,20,10,10,None),group='非必采'),
                     fact(order='O2',warehouse='河北仓',quantities=(30,30,30,20,0),price='3')]
        self.report = {'query':{'from':'2026-09-01','to':'2026-11-05'},'as_of':'2026-11-05T12:00:00+08:00'}

    def book(self, rows=None, report=None):
        stream = io.BytesIO()
        with stream_book() as book:
            m.write_monitor(book,self.rows if rows is None else rows,self.report if report is None else report)
            book.save(stream)
        return openpyxl.load_workbook(io.BytesIO(stream.getvalue()))

    def line(self, sheet, label):
        return next(row for row in sheet if row[0].value==label)

    def test_quantity_separates_units_and_keeps_unknown_and_zero(self):
        self.assertEqual(m.quantity(self.rows,'model','包'),D(40))
        self.assertEqual(m.quantity(self.rows,'model','瓶'),D(20))
        self.assertIsNone(m.quantity(self.rows,'receipt','瓶'))
        self.assertEqual(m.quantity(self.rows[2:],'receipt','包'),D(0))
        self.assertEqual(m.quantity_ratio(self.rows,'payment','receipt','包'),D(2)/D(24))

    def test_quantity_cohort_not_price_dependent_and_no_zero_denominator(self):
        rows=[fact(price=None),fact(sku='other',quantities=(100,100,None,None,None))]
        self.assertEqual(m.quantity_ratio(rows,'signed','return','包'),D(6)/D(8))
        self.assertIsNone(m.quantity_ratio([fact(quantities=(0,0,10,10,0))],'signed','return','包'))
        self.assertEqual(m.quantity_ratio([fact(quantities=(1,1,2,2,0))],'signed','return','包'),2)

    def test_missing_unit_not_summed_and_all_unpriced_not_zero(self):
        rows=[fact(unit=None,price=None)]
        self.assertEqual(m.units(rows),[])
        self.assertIsNone(m.known_value(m.service.aggregate(rows),'model'))
        b=self.book(rows)
        try:
            self.assertEqual(self.line(b['漏斗汇总'],'库存单位未留存的纳入条目')[1].value,1)
            self.assertIsNone(self.line(b['漏斗汇总'],'模型建议已计价货值（元）')[1].value)
            self.assertEqual(self.line(b['漏斗汇总'],'模型建议缺价条目')[1].value,1)
        finally:b.close()

    def test_layout_order_values_percent_text_ids_and_shared_style(self):
        before=copy.deepcopy(self.rows);b=self.book()
        try:
            self.assertEqual(b.sheetnames,['漏斗汇总','必采漏斗','非必采漏斗','属性待核漏斗','门店数量记录','门店货值记录'])
            s=b['漏斗汇总']
            self.assertEqual([s.cell(4,i).value for i in range(1,6)],['业务指标','大盘','山东仓','河北仓','统计口径 / 阅读提示'])
            self.assertEqual(s['A1'].fill.fgColor.rgb,'00142B40')
            self.assertEqual(s.freeze_panes,'B5');self.assertIn('A1:E1',str(s.merged_cells))
            row=self.line(s,'模型建议数量（包）');self.assertEqual([c.value for c in row[1:4]],[40,10,30])
            self.assertEqual(self.line(s,'模型建议已计价货值（元）')[1].value,150)
            self.assertEqual(self.line(b['必采漏斗'],'模型建议已计价货值（元）')[1].value,110)
            self.assertEqual(self.line(b['非必采漏斗'],'模型建议已计价货值（元）')[1].value,40)
            records=b['门店数量记录']
            self.assertEqual(records['C5'].value,'0001');self.assertEqual(records['C5'].data_type,'s')
            self.assertEqual(records['B5'].value,'=测试门店');self.assertEqual(records['B5'].data_type,'s')
            self.assertEqual(records['M5'].number_format,m.PERCENT)
            self.assertEqual(records['M5'].value,.8)
            self.assertEqual(records['H5'].number_format,m.INTEGER)
        finally:b.close()
        self.assertEqual(self.rows,before)

    def test_exclusions_and_receipt_common_cohort(self):
        rows=self.rows+[fact(order='bad',quantities=(10000,)*5,scope='排除'),fact(order='unknown',scope='待核')]
        b=self.book(rows)
        try:
            s=b['漏斗汇总']
            self.assertEqual(self.line(s,'模型建议数量（包）')[1].value,40)
            self.assertEqual(self.line(s,'已记录实收样本的下单货值（元）')[1].value,68)
            self.assertEqual(self.line(s,'已记录实收样本的实收货值（元）')[1].value,4)
            self.assertEqual(self.line(s,'已有完整实收记录的订单数')[1].value,1)
            self.assertEqual(self.line(s,'实收资料覆盖率')[1].value,.5)
        finally:b.close()

    def test_periods_cover_only_full_windows_and_numeric_percent(self):
        self.assertEqual(str(m.period_windows(self.report['as_of'])[0][1]),'2026-10-26')
        rows=[fact(day='2026-10-30'),fact(order='O2',day='2026-10-20',quantities=(5,)*5)]
        b=self.book(rows)
        try:
            row=self.line(b['漏斗汇总'],'大盘 · 模型建议货值（元）')
            self.assertEqual([c.value for c in row[1:4]],[20,10,1])
            self.assertEqual(row[3].data_type,'n');self.assertEqual(row[3].number_format,m.PERCENT)
        finally:b.close()
        r=copy.deepcopy(self.report);r['query']['from']='2026-11-01'
        b=self.book(rows,r)
        try:
            row=self.line(b['漏斗汇总'],'大盘 · 模型建议货值（元）')
            self.assertEqual([c.value for c in row[1:4]],[None]*3)
            self.assertIn('未覆盖',row[4].value)
        finally:b.close()

    def test_cancel_during_monitor_rows(self):
        stop=threading.Event()
        with self.assertRaises(Cancelled):
            with stream_book() as book:
                m.write_monitor(book,self.rows,self.report,lambda _:stop.set(),stop)

if __name__=='__main__':unittest.main()
