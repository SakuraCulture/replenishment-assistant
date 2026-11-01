"""Portable directory fixtures: header drift, source retention, no implicit store-name joins."""
import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import aoxiang_directory as directory
import import_templates


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = directory.Directory(self.root / 'settings')

    def tearDown(self):
        self.temp.cleanup()

    def workbook(self, rows, header=('门店名称', '忽略列', '门店编码'), preamble=False):
        path = self.root / 'stores.xlsx'
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = '门店'
        if preamble:
            sheet.append(['门店导出'])
            sheet.append([])
        sheet.append(header)
        for row in rows:
            sheet.append(row)
        book.save(path)
        book.close()
        return path

    def test_headers_not_positions_and_exact_code_order(self):
        path = self.workbook([['测试店', 4, '0001'], ['测试店', 8, 'T2'], ['长编码店', None, '12345678901234567890']], preamble=True)
        result = directory.read_directory(path)
        self.assertEqual([i['code'] for i in result['items']], ['0001', 'T2', '12345678901234567890'])
        self.assertEqual(result['items'][0]['source_rows'], [4])
        self.assertEqual(result['source']['header_row'], 3)

    def test_duplicate_same_keeps_sources_conflict_blocks(self):
        path = self.workbook([['店1', None, '001'], ['店1', None, '001']])
        result = directory.read_directory(path)
        self.assertEqual(result['items'][0]['source_rows'], [2, 3])
        self.assertEqual(result['duplicate_rows'], 1)
        path = self.workbook([['店1', None, '001'], ['店2', None, '001']])
        with self.assertRaisesRegex(ValueError, '不同名称'):
            directory.read_directory(path)

    def test_invalid_missing_and_duplicate_headers(self):
        for rows in [[['店', None, 1234567890123456]], [['店', None, None]], [[None, None, 'W1']], [['店', None, ' W1']]]:
            with self.assertRaises(ValueError):
                directory.read_directory(self.workbook(rows))
        path = self.workbook([], header=['门店编码', '门店名称', '门店编码'])
        with self.assertRaisesRegex(ValueError, '重复门店表头'):
            directory.read_directory(path)

    def test_bad_excel_dimension_still_reads_physical_rows(self):
        path = self.workbook([['店1', None, 'W1'], ['店2', None, 'W2']])
        with zipfile.ZipFile(path) as source:
            content = {name: source.read(name) for name in source.namelist()}
        content['xl/worksheets/sheet1.xml'] = content['xl/worksheets/sheet1.xml'].replace(b'ref="A1:C3"', b'ref="A1"')
        with zipfile.ZipFile(path, 'w') as target:
            for name, value in content.items():
                target.writestr(name, value)
        self.assertEqual(len(directory.read_directory(path)['items']), 2)

    def test_retained_source_and_mapping_reusable_with_suppliers(self):
        path = self.workbook([['店', None, 'W1']])
        before = path.read_bytes()
        result = directory.read_directory(path)
        saved = self.repo.save(result)
        self.assertEqual(self.repo.load(), saved)
        self.assertEqual(Path(saved['source']['retained_path']).read_bytes(), before)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.repo.suppliers(), {'山东仓': 'ZXC2025', '河北仓': '2078246'})
        self.repo.save_suppliers({'山东仓': 'NEW', '河北仓': '000123'})
        self.assertEqual(self.repo.suppliers()['河北仓'], '000123')
        with self.assertRaises(ValueError):
            self.repo.save_suppliers({'山东仓': 'NEW', '河北仓': 2078246})

    def test_source_changes_before_save_and_corrupt_directory_rejected(self):
        path = self.workbook([['店', None, 'W1']])
        result = directory.read_directory(path)
        path.write_bytes(path.read_bytes() + b'changed')
        with self.assertRaisesRegex(ValueError, '发生变化'):
            self.repo.save(result)
        self.assertFalse(self.repo.path.exists())
        result['items'][0]['code'] = 'other'
        with self.assertRaisesRegex(ValueError, '版本不一致'):
            directory.validate(result)

    def test_change_during_read_rejected(self):
        path = self.workbook([['店', None, 'W1']])
        original = directory.file_hash(path)
        with patch.object(directory, 'file_hash', side_effect=[original, 'changed']):
            with self.assertRaisesRegex(ValueError, '读取期间变化'):
                directory.read_directory(path)

    def test_template_has_only_store_fields_and_specific_instructions(self):
        path = self.root / 'template.xlsx'
        import_templates.write('aoxiang_stores', path)
        book = openpyxl.load_workbook(path)
        try:
            self.assertEqual([c.value for c in book.active[1]], ['门店编码', '门店名称'])
            notes = str(list(book['填写说明'].values))
            self.assertNotIn('回传修订', notes)
            self.assertNotIn('数量单位', notes)
            self.assertIn('按编码关联', notes)
        finally:
            book.close()


if __name__ == '__main__':
    unittest.main()
