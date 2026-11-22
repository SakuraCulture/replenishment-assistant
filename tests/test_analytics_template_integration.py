import sys
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import openpyxl

sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).parent)]
import order_analytics_exports as exports
import test_order_analytics as examples
from runtime_support import Cancelled


class ExportIntegration(unittest.TestCase):
    def setUp(self):self.f=examples.AnalyticsTests();self.f.setUp()
    def tearDown(self):self.f.tearDown()
    def read(self,path,data_only=True):return openpyxl.load_workbook(path,read_only=True,data_only=data_only)

    def test_actual_export_entry_layout_formulas_cached_values_and_scoped_order(self):
        report=self.f.report(); head=report['heads'][0]
        for kind,names,header_row,width in [('analysis',['SKU种类','商品货值','商品数量'],16,22),('analysis_detail',['SKU完整明细'],7,47)]:
            with self.subTest(kind=kind):
                target=self.f.root/(kind+'.xlsx');before=self.f.f.old_tables()
                receipt=exports.export(self.f.path,report,target,kind,[head['number']])
                self.assertEqual(self.f.f.old_tables(),before)
                self.assertEqual(receipt['sheets'],names)
                for cached in (False,True):
                    book=self.read(target,cached)
                    try:
                        self.assertEqual(book.sheetnames,names)
                        for s in book:
                            s.reset_dimensions()
                            headers=[c.value for c in next(s.iter_rows(min_row=header_row,max_row=header_row))]
                            self.assertEqual(len(headers),width)
                            self.assertEqual(headers[-2:],['聚水潭内部订单号','翱象采购单号'])
                            data=list(s.iter_rows(min_row=header_row+1,values_only=True))
                            self.assertTrue(data)
                            order_index=1 if kind=='analysis' else 0
                            self.assertTrue(all(r[order_index]==head['number'] for r in data))
                            if kind=='analysis':
                                self.assertEqual([r[4] for r in data],['ALL','必采','非必采'])
                                self.assertEqual(s['M17'].value,1 if cached else
                                    '=IF(AND(ISNUMBER(G17),G17=0),"基数为0",'
                                    'IF(OR(H17="不适用",G17="不适用"),"不适用",IF(COUNT(H17,G17)<2,"待核",H17/G17)))')
                            else:
                                self.assertIsInstance(data[0][2],str)
                                self.assertEqual(s['P8'].value,1 if cached else
                                    '=IF(AND(ISNUMBER(F8),F8=0),"基数为0",'
                                    'IF(OR(G8="不适用",F8="不适用"),"不适用",IF(COUNT(G8,F8)<2,"待核",G8/F8)))')
                    finally:book.close()

    def test_detail_export_keeps_zero_model_with_signing_and_omits_excluded(self):
        signed_rows = self.f.p['result']['rows']
        zero_model = deepcopy(signed_rows[0])
        zero_model.update(E='0002', AS=0, _execution_qty=2)
        signed_rows.insert(1, zero_model)
        self.f.set_catalog('山东仓', '标杆淘汰（夏季）')
        self.f.save_payload()
        self.f.f.save('return', [{'门店确认数量': 0}, {'商品编码': 'EXTRA', '门店确认数量': 3}])
        report = self.f.report()
        self.assertEqual([(r['warehouse'], r['sku'], r['scope'], r['quantities']['model']) for r in report['rows']],
                         [('河北仓', '0001', '纳入', 24), ('河北仓', '0002', '纳入', 0),
                          ('河北仓', 'EXTRA', '待核', None), ('山东仓', '0002', '排除', 24)])
        target = self.f.root / 'filtered.xlsx'
        import order_analytics
        overview = order_analytics.collect_overview(self.f.path, self.f.spec)
        receipt = exports.export(self.f.path, overview, target, 'analysis_detail')
        self.assertEqual((receipt['rows'], receipt['pending']), (2, 1))
        book = openpyxl.load_workbook(target, data_only=True)
        try:
            sheet = book['SKU完整明细']
            self.assertEqual(sheet.auto_filter.ref, 'A7:AU10')
            self.assertEqual([sheet[f'C{row}'].value for row in range(8, 11)], ['0001', '0002', 'EXTRA'])
            self.assertEqual(sheet['Q8'].value, 0)  # Confirmed store return of 0 / positive signing.
            self.assertEqual(sheet['R8'].value, '基数为0')
            self.assertEqual((sheet['F9'].value, sheet['G9'].value), (0, 2))
            self.assertEqual(sheet['P9'].value, '基数为0')
            self.assertIn('范围：待核', sheet['E10'].value)
        finally:
            book.close()

        excluded_order = next(h['number'] for h in report['heads'] if h['warehouse'] == '山东仓')
        empty = self.f.root / 'filtered-empty.xlsx'
        receipt = exports.export(self.f.path, overview, empty, 'analysis_detail', [excluded_order])
        self.assertEqual((receipt['rows'], receipt['pending']), (0, 0))
        book = openpyxl.load_workbook(empty, data_only=True)
        try:
            self.assertEqual(book['SKU完整明细'].auto_filter.ref, 'A7:AU7')
            self.assertEqual(book['SKU完整明细'].max_row, 7)
        finally:
            book.close()

    def test_existing_target_cancel_and_snapshot_change_never_publish(self):
        report=self.f.report();target=self.f.root/'existing.xlsx';target.write_bytes(b'keep-original')
        with self.assertRaises(ValueError):exports.export(self.f.path,report,target,'analysis')
        self.assertEqual(target.read_bytes(),b'keep-original')
        event=threading.Event();event.set();cancelled=self.f.root/'cancelled.xlsx'
        with self.assertRaises(Cancelled):exports.export(self.f.path,report,cancelled,'analysis',cancel=event)
        self.assertFalse(cancelled.exists())
        import analytics_template_export as writer
        original=writer.append_summary
        def changed(*args,**kwargs):
            result=original(*args,**kwargs);self.f.f.save('return');return result
        stale=self.f.root/'stale.xlsx'
        with patch.object(writer,'append_summary',side_effect=changed):
            with self.assertRaises(Exception):exports.export(self.f.path,report,stale,'analysis')
        self.assertFalse(stale.exists())
        self.assertFalse(list(self.f.root.glob('.analytics-*')))

    def test_order_outside_snapshot_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'不在当前查询范围'):
            exports.export(self.f.path,self.f.report(),self.f.root/'unknown.xlsx','analysis',['not-current-order'])


class ExportMenu(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import test_order_analytics_ui as fixture
        fixture.AnalyticsUiTests.setUpClass()
    def setUp(self):
        import test_order_analytics_ui as fixture
        self.f=fixture.AnalyticsUiTests();self.f.setUp();self.widget=self.f.widget
    def tearDown(self):self.f.tearDown()
    def test_titles_and_filename_follow_selected_template(self):
        w=self.widget;w.show_report(self.f.f.report())
        self.assertEqual([(w.export_kind.itemText(n),w.export_kind.itemData(n)) for n in range(w.export_kind.count())],
                         [('订单经营分析','analysis'),('订单经营细则分析','analysis_detail'),('SKU订单完成率汇总','sku_completion')])
        for index,prefix in enumerate(('订单经营分析','订单经营细则分析','SKU订单完成率汇总')):
            with self.subTest(index=index):
                calls=[];w.export_kind.setCurrentIndex(index)
                with patch('export_settings.load',return_value=str(self.f.f.root)),patch.object(w,'start_task',side_effect=lambda *a,**kw:calls.append((a,kw))):w.export(False)
                args=calls[0][0][1]
                self.assertTrue(Path(args[2]).name.startswith(prefix+'_'))
                self.assertEqual(args[3],w.export_kind.currentData())
    def test_selected_scope_and_drill_scope_preserved(self):
        w=self.widget;w.show_report(self.f.f.report());head=w.report['heads'][0];w.selected={head['number']}
        calls=[]
        with patch('export_settings.load',return_value=str(self.f.f.root)),patch.object(w,'start_task',side_effect=lambda *a,**kw:calls.append((a,kw))):w.export(True)
        self.assertEqual(calls[0][0][1][4],[head['number']])


if __name__=='__main__':unittest.main()
