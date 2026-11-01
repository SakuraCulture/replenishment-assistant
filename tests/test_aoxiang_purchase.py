import copy
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src')]
import test_lifecycle as lifecycle_fixture
from aoxiang_purchase import freeze_payment, validate_basis, fingerprint
from aoxiang_purchase_excel import generate
from aoxiang_directory import file_hash
from aoxiang_jobs import encoded


def config():
    return {'configured': True, 'enabled': True, 'account_binding': {'account_id': 'A', 'merchant_code': 'M'},
            'store_mapping_version': 'mapping-1', 'targets': [{'buyerWarehouseCode': 'T', 'displayName': '测试店'}]}


class PurchaseFixture:
    def setUp(self):
        self.f = lifecycle_fixture.LifecycleTests()
        self.f.setUp()
        self.suppliers = {'山东仓': 'ZXC2025', '河北仓': '2078246'}

    def tearDown(self):
        self.f.tearDown()

    def paid(self, **row):
        self.f.save('return')
        self.f.save('payment', [{'付款单商品数量': 7, '实际金额': .99, **row}])
        return freeze_payment(self.f.path, self.f.h, config(), self.suppliers)


class PurchaseBasisTests(PurchaseFixture, unittest.TestCase):

    def test_frozen_base_quantity_amount_exact_no_lifecycle_mutation(self):
        self.f.save('return')
        self.f.save('payment', [{'付款单商品数量': 2, '数量单位': '箱', '折合库存单位系数': 24, '换算依据': '来源写明24包/箱', '实际金额': .99}])
        before = self.f.view()
        facts = self.f.old_tables()
        plan = freeze_payment(self.f.path, self.f.h, config(), self.suppliers)
        self.assertEqual(plan['rows'][0]['quantity'], '48')
        self.assertEqual(plan['rows'][0]['amount'], '0.99')
        self.assertEqual(plan['supplier_code'], '2078246')
        self.assertEqual(plan['post_creation_state'], '待提交')
        self.assertFalse(plan['create_external_order'])
        self.assertTrue(validate_basis(self.f.path, plan, config(), self.suppliers))
        self.assertEqual(self.f.view(), before)
        self.assertEqual(self.f.old_tables(), facts)

    def test_unknown_store_pending_payment_or_currency_rejected(self):
        self.f.save('payment')
        with self.assertRaisesRegex(ValueError, '尚未确认'):
            freeze_payment(self.f.path, self.f.h, config(), self.suppliers)
        self.f.save('return')
        self.f.save('payment', [{'币种': 'USD'}])
        with self.assertRaisesRegex(ValueError, '币种'):
            freeze_payment(self.f.path, self.f.h, config(), self.suppliers)
        c = config()
        c['targets'][0]['buyerWarehouseCode'] = 'OTHER'
        with self.assertRaisesRegex(ValueError, '门店编码'):
            freeze_payment(self.f.path, self.f.h, c, self.suppliers)

    def test_payment_return_or_config_changed_after_preview_blocks(self):
        plan = self.paid()
        self.f.save('payment', [{'付款单商品数量': 2, '实际金额': .99}])
        with self.assertRaisesRegex(ValueError, '付款单已更新'):
            validate_basis(self.f.path, plan, config(), self.suppliers)
        plan = freeze_payment(self.f.path, self.f.h, config(), self.suppliers)
        self.f.save('return', [{'门店确认数量': 3}])
        with self.assertRaisesRegex(ValueError, '回传对照依据变化'):
            validate_basis(self.f.path, plan, config(), self.suppliers)
        changed = {**self.suppliers, '河北仓': 'changed'}
        with self.assertRaisesRegex(ValueError, '配置已变化'):
            validate_basis(self.f.path, plan, config(), changed)

    def test_zero_paid_quantity_not_created_and_nonzero_amount_not_dropped(self):
        self.f.save('return')
        self.f.save('payment', [{'付款单商品数量': 0, '实际金额': 0}])
        with self.assertRaisesRegex(ValueError, '无需创建'):
            freeze_payment(self.f.path, self.f.h, config(), self.suppliers)
        self.f.save('payment', [{'付款单商品数量': 0, '实际金额': 2}])
        with self.assertRaisesRegex(ValueError, '金额不为0'):
            freeze_payment(self.f.path, self.f.h, config(), self.suppliers)


class PurchaseWorkbookTests(PurchaseFixture, unittest.TestCase):
    """Portable minimal native-like synthetic template, with hidden data and custom ZIP members."""
    def setUp(self):
        super().setUp()
        import openpyxl
        from openpyxl.styles import Font, PatternFill
        from openpyxl.worksheet.datavalidation import DataValidation
        self.contract = json.loads((ROOT / 'components/aoxiang/assets/purchase-import-template-contract.json').read_text(encoding='utf-8'))
        self.contract_path = ROOT / 'components/aoxiang/assets/purchase-import-template-contract.json'
        self.template = self.f.root / 'synthetic-template.xlsx'
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'sheet1'
        for cell in self.contract['schema']['sheets'][0]['headers']:
            ws[cell['ref']] = cell['value']
        for row in [8, 9]:
            for column in range(1, 13):
                ws.cell(row, column, '示例')
                ws.cell(row, column).font = Font(bold=True)
                ws.cell(row, column).fill = PatternFill('solid', fgColor='AAFFAA')
        hidden = wb.create_sheet('hidden_sheet1_col_10')
        hidden.sheet_state = 'hidden'
        hidden['K3001'], hidden['K3002'] = '是', '否'
        validation = DataValidation(type='list', formula1='hidden_sheet1_col_10')
        ws.add_data_validation(validation)
        validation.add('K2:K3001')
        wb.save(self.template)
        wb.close()
        self.receipt = {'status': 'SUPPORTED', 'file': str(self.template), 'sha256': file_hash(self.template),
                        'contractVersion': self.contract['version'], 'schema': copy.deepcopy(self.contract['schema']),
                        'identity': {'accountId': 'A', 'merchantCode': 'M'}, 'uiProductLimit': 100}

    def test_only_input_sheet_changes_text_codes_amount_authoritative(self):
        import openpyxl
        plan = self.paid()
        before = file_hash(self.template)
        out = self.f.root / '采购.xlsx'
        receipt = generate(plan, self.receipt, self.contract_path, out)
        self.assertEqual(file_hash(self.template), before)
        with zipfile.ZipFile(self.template) as a, zipfile.ZipFile(out) as b:
            self.assertEqual(a.namelist(), b.namelist())
            changed = [n for n in a.namelist() if a.read(n) != b.read(n)]
            self.assertEqual(changed, [receipt['modified_zip_member']])
            ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            old = ET.fromstring(a.read(changed[0]))
            new = ET.fromstring(b.read(changed[0]))
            self.assertEqual(ET.tostring(old.find('s:dataValidations', ns)), ET.tostring(new.find('s:dataValidations', ns)))
        wb = openpyxl.load_workbook(out)
        try:
            ws = wb['sheet1']
            self.assertEqual(ws.max_row, 8)
            self.assertEqual([ws.cell(8, c).value for c in range(1, 13)],
                ['T', '2078246', '0001', 7, None, .99, None, '测试商品', None, plan['head']['number'], '否', None])
            self.assertEqual([ws[c].data_type for c in ['A8','B8','C8']], ['s', 's', 's'])
            self.assertTrue(ws['A8'].font.bold)
            self.assertEqual(wb['hidden_sheet1_col_10'].sheet_state, 'hidden')
        finally:
            wb.close()

    def test_hash_limit_tamper_and_precision_block_no_output(self):
        plan = self.paid()
        out = self.f.root / 'blocked.xlsx'
        changed = copy.deepcopy(plan)
        changed['rows'][0]['quantity'] = '1.0000000000000001'
        changed['fingerprint'] = fingerprint(changed)
        with self.assertRaisesRegex(ValueError, '数值精度'):
            generate(changed, self.receipt, self.contract_path, out)
        self.assertFalse(out.exists())
        bad = {**self.receipt, 'uiProductLimit': 0}
        with self.assertRaisesRegex(ValueError, '上限'):
            generate(plan, bad, self.contract_path, out)
        with self.template.open('ab') as f:
            f.write(b'changed')
        with self.assertRaisesRegex(ValueError, '模板在核验后发生变化'):
            generate(plan, self.receipt, self.contract_path, out)
        self.assertFalse(out.exists())


if __name__ == '__main__':
    unittest.main()
