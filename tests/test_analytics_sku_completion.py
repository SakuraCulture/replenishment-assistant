import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).parent)]
import order_analytics
import order_analytics_exports as exports
from analytics_sku_completion import HEADERS
import test_order_analytics as examples


def fact(sku, demand, ordered, *, order='BH-A-HB', store='A', day='2026-11-18',
         name='商品', model=1, scope='纳入'):
    quantities = dict(model=model, signed=0, operations=0, receipt=0, **{'return': demand, 'payment': ordered})
    return dict(sku=sku, order=order, store=store, day=day, name=name, scope=scope, quantities=quantities,
                valid={key: value is not None for key, value in quantities.items()})


class SkuCompletion(unittest.TestCase):
    def write(self, folder, rows):
        target = Path(folder) / 'sku.xlsx'
        report = {'query': {'from': '2026-11-17', 'to': '2026-11-18'}, 'snapshot_id': 'test'}
        exports.export_template(None, report, target, 'sku_completion', rows, lambda _: None, None, _check=lambda: None)
        return target

    def test_weighted_rate_distinct_orders_and_stores_names_precision_and_layout(self):
        sku = '1548253262921322591'
        rows = [fact(sku, 6, 2, day='2026-11-17', name='旧名'),
                fact(sku, 4, 3, order='BH-A-SD', name='新名'),
                fact(sku, 0, 0, order='BH-B-SD', store='B', name='同日别名'),
                fact('00009', 2, 4, name='=文本'), fact('00007', 0, 1), fact('x', 0, 0, model=0),
                fact('excluded', 100, 100, scope='排除')]
        with tempfile.TemporaryDirectory() as folder:
            path = self.write(folder, rows)
            with closing(load_workbook(path, data_only=True)) as book:
                self.assertEqual(book.sheetnames, ['SKU汇总'])
                sheet = book.active
                self.assertEqual(list(sheet.values)[6], HEADERS)
                self.assertEqual((sheet.freeze_panes, sheet.tables['SkuOrders'].ref), ('A8', 'A7:G10'))
                self.assertEqual(list(sheet.values)[7:], [
                    (sku, '新名', 10, 5, 0.5, 2, 1), ('00009', '=文本', 2, 4, 2, 1, 1), ('00007', '商品', 0, 1, None, 0, 0)])
                self.assertEqual(sheet['A8'].number_format, '@')
                self.assertEqual(sheet['B9'].data_type, 's')
                self.assertEqual(sheet['E5'].number_format, '0.00%')
                self.assertEqual([sheet[k].value for k in ('C5','D5')], [12,10])
                self.assertAlmostEqual(sheet['E5'].value, 10 / 12)
            with closing(load_workbook(path)) as book:
                self.assertIn('D5/C5', book.active['E5'].value)
                self.assertIn('SUM(C8:C10)', book.active['C5'].value)

    def test_partial_or_missing_stages_never_converted_to_zero(self):
        rows = [fact('a', 1.25, 1), fact('a', 1, None, order='BH-A-SD'),
                fact('b', None, 3), fact('c', 0, None), fact('d', 9, 7, scope='待核')]
        with tempfile.TemporaryDirectory() as folder, closing(load_workbook(self.write(folder, rows), data_only=True)) as book:
            sheet = book.active
            values = {r[0]: r[2:] for r in list(sheet.values)[7:]}
            self.assertEqual(values, {
                'a': (2.25, '待核', '待核', 2, 1), 'b': ('待核', 3, '待核', '待核', '待核'),
                'c': (0, '待核', None, 0, 0), 'd': ('待核',) * 5})
            self.assertEqual([sheet[k].value for k in ('C5','D5','E5')], ['待核']*3)

    def test_empty_cohort(self):
        with tempfile.TemporaryDirectory() as folder, closing(load_workbook(self.write(folder, []), data_only=True)) as book:
            sheet = book.active
            self.assertEqual([sheet[k].value for k in ('C5','D5','E5')], [0,0,None])
            self.assertEqual(sheet.tables['SkuOrders'].ref, 'A7:G7')

    def test_overview_and_selected_order_use_frozen_stages_and_snapshot_validation(self):
        f = examples.AnalyticsTests()
        f.setUp()
        try:
            f.f.save('return')
            f.f.save('payment')
            overview = order_analytics.collect_overview(f.path, f.spec)
            selected = f.f.h['number']
            target = f.root / 'selected.xlsx'
            with patch.object(order_analytics, 'collect_view', side_effect=AssertionError('expanded projection')):
                result = exports.export(f.path, overview, target, 'sku_completion', [selected])
            self.assertEqual((result['template'], result['rows']), ('sku_completion', 1))
            with closing(load_workbook(target, data_only=True)) as book:
                self.assertEqual(list(book.active.values)[7][2:], (5,5,1,1,1))
            with self.assertRaisesRegex(ValueError,'不在当前查询范围'):
                exports.export(f.path, overview, f.root / 'outside.xlsx', 'sku_completion', ['unknown'])
            with self.assertRaisesRegex(ValueError,'请先勾选'):
                exports.export(f.path, overview, f.root / 'empty-selection.xlsx', 'sku_completion', [])
            f.f.save('return', [{'门店确认数量':7}])
            stale = f.root / 'stale.xlsx'
            with self.assertRaises(Exception):
                exports.export(f.path, overview, stale, 'sku_completion', [selected])
            self.assertFalse(stale.exists())
        finally:
            f.tearDown()
