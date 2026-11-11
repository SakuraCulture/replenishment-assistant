"""Export-only field removal preserves screen detail and frozen five-stage facts."""
import copy,unittest
from decimal import Decimal
import openpyxl
import test_order_analytics as seed
from test_funnel_dimensions import enrich
import funnel_dimension_export as dimensions
import order_analytics_exports as exports

class BusinessExportFields(unittest.TestCase):
    def setUp(self):
        self.f=seed.AnalyticsTests();self.f.setUp();self.addCleanup(self.f.tearDown);enrich(self.f)
    def test_all_native_export_levels_omit_only_requested_fields(self):
        report=self.f.report();before=copy.deepcopy(report)
        for level in ('store','order','detail','coverage_rounds'):
            file=self.f.root/(level+'.xlsx');exports.export(self.f.path,report,file,level=level)
            book=openpyxl.load_workbook(file,read_only=True,data_only=True)
            try:
                for sheet in book:
                    heads=next(sheet.values,())
                    self.assertTrue(dimensions.OMITTED_HEADERS.isdisjoint(heads),(level,sheet.title))
                    if sheet.title in dimensions.NAMES:
                        self.assertEqual(list(heads),dimensions.headers(sheet.title))
                if level=='detail':
                    rows=[r for r in report['rows'] if r['scope']=='纳入']
                    expected=[[None if v=='' else float(v) if isinstance(v,Decimal) else v for v in r] for r in exports.export_detail_rows(rows)]
                    self.assertEqual([list(r) for r in list(book['全量商品五段'].values)[1:]],expected)
                    self.assertIn('大仓上游供应商',next(book['全量商品五段'].values))
                    self.assertIn('商品类目',next(book['全量商品五段'].values))
            finally:book.close()
        self.assertEqual(report,before)
    def test_screen_detail_columns_and_identifiers_remain_unchanged(self):
        rows=self.f.report()['rows'];shown=list(exports.detail_rows(rows));exported=list(exports.export_detail_rows(rows))
        self.assertTrue({'供应商资料状态','类目资料状态','维度资料依据'}<=set(exports.DETAIL_HEADERS))
        for original,output in zip(shown,exported):
            self.assertEqual(len(original),len(exports.DETAIL_HEADERS));self.assertEqual(len(output),len(exports.EXPORT_DETAIL_HEADERS))
            self.assertEqual(output,[original[i] for i in exports.EXPORT_DETAIL_INDEXES]);self.assertEqual(output[-3:],original[-3:])
