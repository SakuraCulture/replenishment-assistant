"""Lifecycle ranges use synthetic signed orders and the real query/export path."""
import sqlite3
from contextlib import closing
from unittest.mock import patch
import unittest

import openpyxl
import test_order_analytics as fixtures
import order_analytics as analytics
import order_analytics_exports as exports
import operations_flow


class LifecycleScopeTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.AnalyticsTests()
        self.f.setUp()
        self.backup = patch('storage_chunks.backup_before_write')
        self.backup.start()

    def tearDown(self):
        self.backup.stop()
        self.f.tearDown()

    def report(self, value='', **query):
        return self.f.report(lifecycle_from=value, **query)

    def test_default_keeps_all_signed_orders_without_requiring_returns(self):
        old, new = self.f.report(), self.report()
        for key in ('rows', 'heads', 'payments', 'metrics', 'groups', 'orders', 'workflow'):
            self.assertEqual(old[key], new[key], key)
        self.assertEqual(len(new['heads']), 2)
        self.assertFalse(self.report('payment')['heads'])

    def test_entering_payment_includes_unconfirmed_payment_and_only_that_warehouse(self):
        self.f.f.save('return', [{'门店确认数量': 12}])
        before = self.f.path.read_bytes()
        report = self.report('payment')
        self.assertEqual(len(report['heads']), 1)
        self.assertEqual(report['heads'][0]['warehouse'], '河北仓')
        row = report['rows'][0]
        self.assertEqual(row['order_status'], '待付款')
        self.assertEqual(row['quantities']['return'], 12)
        self.assertIsNone(row['quantities']['payment'])
        self.assertEqual(len(report['orders']), 1)
        self.assertEqual(report['metrics']['included_orders'], 1)
        self.assertFalse(self.report('receipt')['heads'])
        self.assertEqual(self.f.path.read_bytes(), before)

    def test_payment_range_keeps_later_receipt_and_completed_orders(self):
        self.f.f.save('return')
        self.f.f.save('payment', [{'付款单商品数量': 2}])
        self.assertEqual(len(self.report('payment')['heads']), 1)
        self.assertEqual(len(self.report('receipt')['heads']), 1)
        self.assertFalse(self.report('finished')['heads'])
        self.f.f.save('receipt', [{'实际收货数量': 1}])
        self.assertEqual(len(self.report('payment')['heads']), 1)
        self.assertEqual(len(self.report('receipt')['heads']), 1)
        self.assertEqual(len(self.report('finished')['heads']), 1)

    def test_early_zero_demand_end_does_not_qualify_as_payment(self):
        self.f.f.save('return', [{'门店确认数量': 0}])
        self.assertEqual(len(self.report('finished')['heads']), 1)
        self.assertFalse(self.report('payment')['heads'])
        self.assertFalse(self.report('receipt')['heads'])

    def test_current_operations_route_respects_pending_operations(self):
        self.f.f.save('return')
        head = self.f.f.h
        with closing(sqlite3.connect(self.f.path)) as db, db:
            operations_flow.schema(db)
            db.execute('INSERT OR REPLACE INTO lifecycle_routes VALUES(?,?,2)', (head['oid'], head['revision']))
        self.assertEqual(len(self.report('operations')['heads']), 1)
        self.assertFalse(self.report('payment')['heads'])
        self.f.f.save('operations')
        self.assertEqual(len(self.report('payment')['heads']), 1)

    def test_stale_payment_is_still_in_pending_payment_range(self):
        self.f.f.save('return', [{'门店确认数量': 12}])
        self.f.f.save('payment', [{'付款单商品数量': 2}])
        self.f.f.save('return', [{'门店确认数量': 10}])
        report = self.report('payment')
        self.assertEqual(len(report['heads']), 1)
        self.assertFalse(report['rows'][0]['valid']['payment'])
        self.assertFalse(self.report('receipt')['heads'])

    def test_historical_cutoff_uses_progress_at_that_time(self):
        self.f.f.save('return')
        self.f.f.save('payment')
        day = self.f.spec['from']
        with closing(sqlite3.connect(self.f.path)) as db, db:
            db.execute('UPDATE normal_revisions SET saved=?', (day+'T08:00:00+08:00',))
            db.execute("UPDATE lifecycle_versions SET saved=? WHERE stage='return'", (day+'T10:00:00+08:00',))
            db.execute("UPDATE lifecycle_versions SET saved=? WHERE stage='payment'", (day+'T12:00:00+08:00',))
        self.assertFalse(self.report('payment', as_of=day+'T09:00:00+08:00')['heads'])
        ready = self.report('payment', as_of=day+'T11:00:00+08:00')
        self.assertEqual(len(ready['heads']), 1)
        self.assertFalse(ready['rows'][0]['valid']['payment'])
        self.assertFalse(self.report('receipt', as_of=day+'T11:00:00+08:00')['heads'])

    def test_existing_orders_excluded_by_range_return_empty_instead_of_missing(self):
        head = self.f.f.h
        report = self.report('payment', orders=[head['number']], stores=[head['store']])
        self.assertFalse(report['heads'])
        with self.assertRaisesRegex(ValueError, '不存在'):
            self.report('payment', orders=['NOT-A-SIGNED-ORDER'])
        with self.assertRaisesRegex(ValueError, '生命周期范围'):
            self.report('payment-confirmed')
        from automation_protocol import ANALYTICS_QUERY, validate
        validate({**self.f.spec,'lifecycle_from':'payment'}, ANALYTICS_QUERY)

    def test_both_templates_export_the_same_filtered_orders_and_label(self):
        self.f.f.save('return')
        report = self.report('payment')
        number = report['heads'][0]['number']
        for kind in ('analysis', 'analysis_detail'):
            with self.subTest(kind=kind):
                target = self.f.root / (kind+'.xlsx')
                exports.export(self.f.path, report, target, level=kind)
                book = openpyxl.load_workbook(target, read_only=True, data_only=True)
                try:
                    if kind == 'analysis':
                        for sheet in book:
                            self.assertIn('待付款及以后', sheet['A14'].value)
                            rows = list(sheet.iter_rows(min_row=17, values_only=True))
                            self.assertEqual(len(rows), 3)
                            self.assertEqual({r[1] for r in rows}, {number})
                    else:
                        sheet = book['SKU完整明细']
                        self.assertIn('待付款及以后', sheet['A3'].value)
                        rows = list(sheet.iter_rows(min_row=8, values_only=True))
                        self.assertEqual(len(rows), 1)
                        self.assertEqual(rows[0][0], number)
                finally:
                    book.close()
        excluded = next(h['number'] for h in self.f.report()['heads'] if h['number'] != number)
        with self.assertRaisesRegex(ValueError, '不在当前查询范围'):
            exports.export(self.f.path, report, self.f.root/'outside.xlsx', level='analysis', orders=[excluded])


if __name__ == '__main__':
    unittest.main()
