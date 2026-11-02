"""Portable synthetic Excel/SQLite tests; no real input or user settings required."""
import copy
import pickle
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import openpyxl
import store_parameter_import as spi
import parameter_schema as schema
import import_templates
from runtime_support import Cancelled


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.counter = 0

    def tearDown(self):
        self.tmp.cleanup()

    def sheet(self, headers, rows):
        self.counter += 1
        path = self.root / f'{self.counter}.xlsx'
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(headers)
        for row in rows:
            ws.append(row)
        wb.save(path)
        wb.close()
        return path

    def test_reordered_aliases_zero_blank_long_codes_and_source(self):
        path = self.sheet(['预算金额', '仓库/门店编码', '山东仓备货深度', '库存目标分位概率'],
                          [[0, '00001', 10, None], [None, '12345678901234567890', None, .8]])
        data = spi.read([path, path])
        self.assertEqual([r['code'] for r in data['rows']], ['00001', '12345678901234567890'])
        self.assertTrue(data['rows'][1]['source'].endswith('第3行'))
        p = spi.plan(data, {})
        self.assertFalse(p['errors'])
        v = p['values']['00001']
        self.assertEqual((v['ui']['mode'], v['ui']['amount']), (2, '0'))
        self.assertEqual(v['preset']['config']['warehouses']['山东仓'][0], 10)
        self.assertEqual(v['preset']['config']['parameters']['probability'], schema.standard()['parameters']['probability'])

    def test_template_and_prefilled_roundtrip(self):
        path = self.root / 'template.xlsx'
        import_templates.write('store_parameters', path)
        with self.assertRaisesRegex(ValueError, '没有可导入'):
            spi.read([path])
        records = {'00001': spi.base(), 'T2': spi.base()}
        records['T2']['ui'].update(mode=2, amount='123.45', primary='山东仓', secondary='河北仓')
        records['T2']['preset']['config']['warehouses']['山东仓'] = [12, 6]
        records['T2']['preset']['config']['category_profiles']['自定义'] = '快销'
        spi.write(path, records)
        p = spi.plan(spi.read([path]), records)
        self.assertEqual(p['errors'], [])
        self.assertEqual(p['changed'], [])
        # A full exported row must not silently coerce unchanged ints/tuples.
        for code in records:
            self.assertEqual(pickle.dumps(p['values'][code]['preset']['config']), pickle.dumps(records[code]['preset']['config']))
        with self.assertRaises(FileExistsError):
            import_templates.write('store_parameters', path)

    def test_multiple_files_order_duplicates_and_header_ambiguity(self):
        a = self.sheet(['门店编码', '预算金额'], [['B', 4]])
        b = self.sheet(['门店编码', '预算金额'], [['A', 3]])
        self.assertEqual([r['code'] for r in spi.read([a, b])['rows']], ['B', 'A'])
        c = self.sheet(['门店编码', '预算金额'], [['B', 7]])
        with self.assertRaisesRegex(ValueError, '重复'):
            spi.read([a, c])
        d = self.sheet(['门店编码', '预算金额', '门店预算'], [['B', 2, 2]])
        with self.assertRaisesRegex(ValueError, '重复'):
            spi.read([d])

    def test_invalid_input_does_not_coerce(self):
        cases = [(['门店编码', '错写的超参'], [['A', 2]], '无法识别'),
                 (['门店编码', '预算金额'], [[12345678901234567, 2]], '15位'),
                 (['门店编码', '预算金额'], [['A', '=1+2']], '公式'),
                 (['门店编码', '预算金额'], [[None, 2]], '必填')]
        for headers, rows, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                spi.read([self.sheet(headers, rows)])
        for value in (-1, float('inf'), float('nan'), True, ''):
            with self.assertRaises(ValueError):
                spi.number(value)

    def test_validation_and_current_missing_name_and_fixed_rules(self):
        for header, val, message in [('库存目标分位概率', 2, '参数无效'),
                                     ('权重/快销/最近7天权重', 1, '不能超过1'),
                                     ('极端缺货边界', 31, '固定30'),
                                     ('主仓', '广州仓', '主从仓'),
                                     ('分类映射/食品', '未存在', '分类映射')]:
            data = spi.read([self.sheet(['门店编码', header], [['A', val]])])
            self.assertIn(message, spi.plan(data, {})['errors'][0])
        data = spi.read([self.sheet(['门店编码', '门店名称'], [['A', '甲店']])])
        self.assertIn('未载入', spi.plan(data, {}, current=True)['errors'][0])
        self.assertIn('不一致', spi.plan(data, {}, names={'A': '乙店'})['errors'][0])

    def test_route_and_budget_are_checked_together(self):
        p = self.sheet(['门店编码', '主仓', '从仓', '预算方式', '预算金额'],
                       [['A', '山东仓', '山东仓', None, None], ['B', None, None, '不限预算', 0]])
        data = spi.read([p])
        self.assertEqual(len(spi.plan(data, {})['errors']), 2)
        p = self.sheet(['门店编码', '从仓', '预算方式'], [['A', '无从仓', '不限预算']])
        original = spi.base(ui={'primary': '山东仓', 'secondary': '河北仓', 'mode': 2, 'amount': '100'})
        result = spi.plan(spi.read([p]), {'A': original})
        self.assertFalse(result['errors'])
        self.assertEqual(result['values']['A']['ui']['secondary'], None)
        self.assertEqual(result['values']['A']['ui']['amount'], '')
        self.assertEqual(original['ui']['secondary'], '河北仓')

    def test_cancel_and_changed_source(self):
        p = self.sheet(['门店编码', '预算金额'], [['A', 2]])
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            spi.read([p], cancel=cancel)
        with patch.object(spi, 'digest', side_effect=['first', 'changed']):
            with self.assertRaisesRegex(ValueError, '文件变化'):
                spi.read([p])

    def test_patch_changes_only_requested_fields(self):
        original = spi.base()
        before = copy.deepcopy(original)
        row = {'values': {('parameters', 'cap_days'): 19}}
        after = spi.patch(row, original)
        self.assertEqual(original, before)
        self.assertEqual(list(spi.changes(original, after)), [('模型超参 / 普通库存额度天数', 21, 19)])
        self.assertEqual(after['preset']['base_config'], before['preset']['config'])


if __name__ == '__main__':
    unittest.main()
