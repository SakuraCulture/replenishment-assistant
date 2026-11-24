"""Regression tests for staged merchant exports and exact historical re-exports."""
import copy
import unittest
from contextlib import closing
from unittest.mock import patch

import openpyxl

import lifecycle_batch
import normal_exports
import normal_orders
import order_identity
import order_lifecycle
import purchases
import warehouse_execution
import work_repository
from test_lifecycle import LifecycleTests
from test_warehouse_execution_readers import WarehouseExecutionReaderTests


def product_rows(book):
    return [row for ws in book if ws.title != order_identity.SHEET for row in list(ws.values)[1:]]


class OrderExportPaths(unittest.TestCase):
    def test_by_warehouse_copies_each_stage_and_streams_in_selected_order(self):
        f = LifecycleTests()
        f.setUp()
        self.addCleanup(f.tearDown)
        other = copy.deepcopy(f.payload)
        other['result'].update(store='T2', store_name='第二家店')
        for row in other['result']['rows']:
            row.update(A='第二家店', B='T2')
        with closing(f.work.connect()) as db, db:
            db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)', (f.h['day'], 'T2', 'normal_model', 'other', 'BH-T2-ZC', 1))
            digest = normal_orders.fingerprint_owned(other['result'])
            blob = normal_orders.encode(other, db)
            db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)', ('other', 1, digest, work_repository.now(), blob))
        heads = purchases.targets(f.path)
        hb2 = next(h for h in heads if h['store'] == 'T2' and h['warehouse'] == '河北仓')
        hb1, sd1 = f.heads
        for stage, quantity in [('return', 5), ('operations', 9)]:
            f.save(stage, [
                {'门店确认数量':0},
                {'商品编码':'0007', '门店确认数量':quantity},
                {'补货单号':hb2['number'], '门店编码':'T2', '门店确认数量':quantity},
                {'补货单号':sd1['number'], '供货仓':'山东仓', '商品编码':'0002', '门店确认数量':quantity},
            ])
            folder = f.root / stage
            folder.mkdir()
            result = order_lifecycle.export(f.path, [hb2, sd1, hb1], folder, stage, 'combined')
            self.assertIsNone(result['error'])
            self.assertIn('河北仓', result['files'][0])
            self.assertIn('山东仓', result['files'][1])
            with closing(openpyxl.load_workbook(result['files'][0])) as book:
                rows = [(r[1], r[4], r[16]) for r in product_rows(book)]
                self.assertEqual(rows, [('T2','0001',quantity),('T','0001',0),('T','0007',quantity)])
                self.assertEqual(book[order_identity.SHEET].sheet_state, 'veryHidden')
                self.assertTrue(book['本月新品'].protection.sheet)
                self.assertFalse(book[normal_exports.SHEETS[-1]].protection.sheet)
                self.assertIn('本版', book[normal_exports.SHEETS[-1]]['Q1'].comment.text)
                self.assertEqual(book[normal_exports.SHEETS[-1]]['P4'].number_format, '0.00')
                self.assertEqual([e['number'] for e in order_identity.read(book) if e['sheet']==normal_exports.SHEETS[0]], [hb2['number'],hb1['number']])

    def test_legacy_original_and_managed_v1_and_v2(self):
        f = WarehouseExecutionReaderTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        legacy = f.root / 'legacy'
        legacy.mkdir()
        old = f.current()[0]
        legacy_file = lifecycle_batch.export_signed(f.path, [old], legacy)[0]
        f.convert()
        def revise(value):
            value['result']['rows'][0]['V'] = 7.99
        f.update_hb_only(revise)
        with closing(f.work.connect()) as db:
            versions = [h for h in warehouse_execution.heads(db, history=True) if h['warehouse'] == '河北仓']
        self.assertEqual([h['revision'] for h in versions], [2,1])
        outputs = []
        for h in versions:
            folder = f.root / str(h['revision'])
            folder.mkdir()
            with patch('normal_orders.Repository.get', side_effect=AssertionError('Unexpected full parent read')):
                outputs.append(lifecycle_batch.export_signed(f.path, [h], folder)[0])
        def saved(file):
            with closing(openpyxl.load_workbook(file)) as workbook:
                return [(r[4], r[15], r[16]) for r in product_rows(workbook)], order_identity.read(workbook)[0]['revision']
        self.assertEqual(saved(outputs[1]), saved(legacy_file))
        self.assertEqual(saved(outputs[0]), ([('0001',7.99,48)], 2))
        with self.assertRaisesRegex(RuntimeError, '文件名重复'):
            lifecycle_batch.export_signed(f.path, versions, f.root)
