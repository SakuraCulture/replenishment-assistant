"""Operating report rules, workbook formulas and frozen source identity."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

import openpyxl

from business_daily import PendingInputs, aggregate, classify
from business_daily_sources import file_source, path_for, sales_rows
from business_daily_workbook import SHEETS, write


def product(stock, status='启用', virtual='', category='食品', supplier='测试供货商'):
    return {'stock': stock, 'status': status, 'virtual': virtual, 'category': category, 'supplier': supplier}


def fixture():
    stores = {
        'A': {'code': 'A', 'name': '测试A', 'tag': '山东-维稳型', 'owner': '甲',
              'primary': '山东仓', 'secondary': None},
        'B': {'code': 'B', 'name': '测试B', 'tag': '河北-维稳型', 'owner': '乙',
              'primary': '河北仓', 'secondary': None},
        'C': {'code': 'C', 'name': '测试C', 'tag': '河北-新店', 'owner': '乙',
              'primary': '河北仓', 'secondary': '山东仓'},
    }
    catalogs = {'山东仓': {'0001': product(0), '0002': product(2), '0003': product(3), '@01': product(5)},
                '河北仓': {'0002': product(4, '禁用'), '0003': product(0, category='日配')}}
    sales = []
    for code, sku, stock, inbound in (
            ('A', '0002', 0.5, 4), ('A', '0001', 0, 0), ('A', '@01', 1, 0),
            ('B', '0001', 0, 0), ('B', '0003', -1, 2), ('B', '0002', 0, 0),
            ('C', '0001', 0, 0), ('C', '@01', 0, 0)):
        sales.append({'store': code, 'sku': sku, 'name': sku, 'status': '正常', 'month': 3,
                      'stock': stock, 'inbound': inbound, 'source': '隔离底稿'})
    return {'stores': stores, 'catalogs': catalogs, 'sales': sales, 'blacklist': {('测试C', '@01')},
            'owners': {'食品': '测试采购'}, 'weeks': {'metrics': {}},
            'must_sell': {'A': {'rate': 0.05, 'owner': '甲', 'group': '华东', 'name': '测试A'},
                          'B': {'rate': 0.15, 'owner': '乙', 'group': '华北', 'name': '测试B'},
                          'OTHER': {'rate': 0.4, 'owner': '丙', 'group': '华东', 'name': '外表独有'}},
            'orders': [
                {'store': 'A', 'warehouse': '山东仓', 'order': 'BH-A', 'revision': 1, 'sku': '0001',
                 'demand': 12, 'demand_value': 37.8, 'ordered': 0, 'ordered_value': 0},
                {'store': 'B', 'warehouse': '河北仓', 'order': 'BH-B', 'revision': 2, 'sku': '0003',
                 'demand': 5, 'demand_value': 4.8, 'ordered': 5, 'ordered_value': 4.8},
            ]}


class DailyRules(unittest.TestCase):
    def test_supplier_routes_and_backfall(self):
        data = fixture()
        a, b, c = data['stores'].values()
        self.assertEqual(classify(a, '0001', data['catalogs'])[0], '主仓（山东）')
        self.assertIsNone(classify(b, '0001', data['catalogs'])[0])
        self.assertEqual(classify(c, '0001', data['catalogs'])[0], '从仓（山东）')
        self.assertEqual(classify(c, '0002', data['catalogs'])[0], '河北仓禁采')
        details = []
        result = aggregate(data, details.append)
        self.assertEqual(result['stockout_sizes'], {'山东仓': 1, '河北仓': 1})
        self.assertEqual([r[5] for r in details], ['0001', '0001', '0003', '0002', '0001'])
        self.assertEqual(result['counts']['A'].shortage, 2)
        self.assertEqual(result['counts']['A'].warehouse_shortage, 1)
        self.assertEqual(result['counts']['B'].local_shortage, 1)
        self.assertEqual(result['counts']['B'].active, 1)
        self.assertEqual(result['counts']['B'].warehouse_shortage, 1)
        self.assertEqual(result['discrepancies'], {('A', '可用库存大于0且小于1'): 1, ('C', '地采黑名单'): 1})

    def test_missing_expected_store_and_unknown_inventory(self):
        data = fixture()
        data['sales'][0]['stock'] = None
        with self.assertRaisesRegex(PendingInputs, '可用库存待核'):
            aggregate(data, lambda _: None)
        data = fixture()
        data['sales'] = [x for x in data['sales'] if x['store'] != 'C']
        with self.assertRaisesRegex(PendingInputs, '缺门店底稿：C'):
            aggregate(data, lambda _: None)

    def test_four_sheets_rate_cache_and_offline_formulas(self):
        data = fixture()
        frozen = {'day': '2026-11-23', 'partial': False, 'files': {}, 'expected_stores': list(data['stores']),
                  'order_snapshot': 'frozen-orders', 'order_cutoff': '2026-11-24T12:00:00+08:00',
                  'catalogs': {}, 'store_snapshots': [], 'sales': [], 'order_revisions': []}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'daily.xlsx'
            report = write(path, frozen, data, lambda _: None, None)
            self.assertEqual((report['stores'], report['details'], report['must_sell_stores']), (3, 5, 3))
            with closing(openpyxl.load_workbook(path, keep_links=False)) as formulas:
                self.assertEqual(tuple(formulas.sheetnames), SHEETS)
                self.assertEqual(formulas['门店缺货率'].freeze_panes, 'A3')
                self.assertEqual(formulas['门店缺货率'].auto_filter.ref, 'A2:T5')
                self.assertEqual(formulas['门店缺货明细'].auto_filter.ref, 'A1:Q6')
                self.assertEqual(formulas['汇总']['C20'].data_type, 'f')
                self.assertEqual(formulas['汇总']['G20'].data_type, 'f')
                self.assertEqual(formulas['门店缺货明细']['F2'].value, '0001')
            with closing(openpyxl.load_workbook(path, read_only=True, keep_links=False, data_only=True)) as values:
                self.assertAlmostEqual(values['汇总']['C20'].value, (2 / 3 + 1 + 1) / 3)
                self.assertAlmostEqual(values['汇总']['G20'].value, (0.05 + 0.15 + 0.4) / 3)
                self.assertEqual(values['门店缺货率']['H3'].value, 2 / 3)
                self.assertEqual(values['门店缺货率']['O3'].value, None)
            with ZipFile(path) as zipfile:
                self.assertFalse(any(n.startswith('xl/externalLinks/') for n in zipfile.namelist()))

    def test_supplement_and_duplicate_sales_are_bound_to_exact_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            refs = []
            for i in range(3):
                path = Path(folder) / f'sales{i}.xlsx'
                book = openpyxl.Workbook()
                book.active.append(('仓库/门店编码', '商品编码', '商品名称', '月销', '可用库存', '在途库存', '补货状态'))
                book.active.append(('A', '00001', '隔离测试', 2, 0, 0 if i < 2 else 1, '正常'))
                book.save(path)
                refs.append(file_source(folder, path))
            basis = {'sales': refs[:2]}
            self.assertEqual([r['sku'] for r in sales_rows(folder, basis, None)], ['00001'])
            with self.assertRaisesRegex(ValueError, '两份底稿不同：A'):
                list(sales_rows(folder, {'sales': refs}, None))
            path_for(folder, refs[0]).write_bytes(b'invalid')
            with self.assertRaisesRegex(ValueError, '摘要已变化'):
                path_for(folder, refs[0])
