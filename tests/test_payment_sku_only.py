"""Payment SKU is authoritative; auxiliary codes/barcodes never bind or reject."""
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path[:0]=[str(Path(__file__).resolve().parents[1]/p) for p in ('src','tests')]
import order_lifecycle as lc
import payment_matching as pm
import lifecycle_io as io
import test_payment_matching as fixtures


class SkuOnlyTests(unittest.TestCase):
    def setUp(self):
        self.head={'day':'2026-11-10','store':'S','revision':1,'digest':'signed','warehouse':'河北仓','products':{'A':{},'B':{}}}
        self.order={'digest':'signed','payload':{'result':{'rows':[
            {'E':'A','F':'a-barcode shared','AU':'河北仓'},
            {'E':'B','F':'b-barcode shared','AU':'河北仓'},
            {'E':'C','F':'c-barcode','AU':'山东仓'}]}}}
    def match(self,sku,aux):
        rows=[{'adapter':'paid_order','sku':sku,'source_order':{'内部订单号':'JST-1','店铺名称':'S','辅助码':aux}}]
        with patch('order_lifecycle.repository') as repo:
            repo.return_value.get.return_value=self.order
            lc.match_payment_rows('unused-isolated.sqlite3',self.head,rows)
        return rows[0]
    def test_valid_sku_ignores_conflicting_ambiguous_and_other_warehouse_auxiliary_codes(self):
        for aux in ('b-barcode','shared','c-barcode','unknown',None):
            with self.subTest(aux=aux):
                row=self.match('A',aux)
                self.assertEqual(row['sku'],'A');self.assertEqual(row['matching_basis'],'商品编码精确匹配')
    def test_auxiliary_code_cannot_rewrite_an_unknown_product_code(self):
        row=self.match('UNKNOWN','a-barcode')
        self.assertEqual(row['sku'],'UNKNOWN');self.assertNotIn('source_sku',row)
        self.assertEqual(row['matching_basis'],'清单外商品，需确认')
    def test_unknown_sku_with_ambiguous_auxiliary_code_remains_unmatched(self):
        self.assertEqual(self.match('UNKNOWN','shared')['sku'],'UNKNOWN')
    def test_other_warehouse_product_is_still_rejected_by_product_code(self):
        with self.assertRaisesRegex(ValueError,'商品属于另一供货仓'):self.match('C','a-barcode')
    def test_stale_signed_revision_still_fails(self):
        self.order['digest']='different'
        with self.assertRaises(lc.wr.Conflict):self.match('A','a-barcode')


class SkuOnlyIntegrationTests(unittest.TestCase):
    def setUp(self):self.f=fixtures.PaymentMatchingTests();self.f.setUp()
    def tearDown(self):self.f.tearDown()
    def test_exact_recognition_save_and_repeat_ignore_auxiliary_changes(self):
        doc=self.f.document(change=lambda r:r.update(辅助码='1877239291634511964;UNRELATED'))
        result=pm.analyze(self.f.path,doc,pm.build(self.f.path))
        self.assertTrue(result['automatic'])
        plan=pm.prepare(self.f.path,[doc],None,pm.build(self.f.path),None)
        original=copy.deepcopy(doc['scan']['rows']);self.assertTrue(lc.commit(self.f.path,plan)[0]['changed'])
        for before,after in zip(original,plan['plans'][0]['payload']['raw_rows']):self.assertEqual(before['sku'],after['sku'])
        doc2=self.f.document(change=lambda r:r.update(辅助码='DIFFERENT-AUXILIARY'))
        result2=pm.analyze(self.f.path,doc2,pm.build(self.f.path));self.assertTrue(result2['automatic'])
        self.assertEqual(result['candidates'][0]['number'],result2['candidates'][0]['number'])
    def test_auxiliary_column_is_optional_and_duplicate_auxiliary_headers_do_not_block(self):
        for headers in ([h for h in io.PAYMENT_HEADERS if h!='辅助码'],io.PAYMENT_HEADERS+['辅助码']):
            with self.subTest(headers=headers):
                doc=self.f.document(headers=headers,change=lambda r:r.update(辅助码='IGNORED'))
                self.assertTrue(pm.analyze(self.f.path,doc,pm.build(self.f.path))['automatic'])


if __name__=='__main__':unittest.main()
