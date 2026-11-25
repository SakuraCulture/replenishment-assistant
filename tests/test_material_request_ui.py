"""Store checkboxes and typed material entry share one registration dialog."""
import os
import tempfile
import unittest
from contextlib import closing

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QWidget

import material_requests
import work_repository as wr
from material_ui import RequestEditor, request_edit
from test_material_orders import HB, SD, SKU, catalog, seed


class DemandRegistration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.parent = QWidget()
        self.parent.path = seed(self.folder.name)
        self.addCleanup(self.parent.close)

    def data(self):
        with closing(wr.read_connection(self.parent.path)) as db:
            return material_requests.read(db)

    @staticmethod
    def enter(field, text):
        field.setText(text)
        QTest.keyClick(field,Qt.Key.Key_Return)

    def test_type_and_check_stores_and_goods_then_keep_each_store_request(self):
        editor = RequestEditor(self.parent,preset_warehouse=SD)
        self.assertFalse(editor.save_btn.isEnabled())
        self.enter(editor.stores.search,'店')
        self.assertFalse(editor.stores.checked)
        self.enter(editor.stores.search,'B')
        editor.stores.search.setText('A')
        self.assertTrue(editor.stores.items['B'].isHidden())
        editor.stores.items['A'].setCheckState(Qt.CheckState.Checked)
        self.assertEqual(editor.stores_to_save(),('B','A'))

        editor.lines.item(0,0).setText(SKU)
        editor.lines.item(0,1).setText('5')
        self.enter(editor.products.search,'胶带')
        self.assertEqual(list(editor.draft_rows()),[dict(sku=SKU,quantity='5',note='')])
        self.assertEqual(set(editor.products.items),{SKU,'@ZERO'})
        self.assertIn('可用 0',editor.products.items['@ZERO'].text())
        editor.products.search.clear()
        editor.products.items[SKU].setCheckState(Qt.CheckState.Checked)
        editor.products.items['@ZERO'].setCheckState(Qt.CheckState.Checked)
        editor.products.search.setText('扎带')
        editor.add_checked.click()
        editor.lines.item(1,1).setText('2')
        self.assertEqual(editor.save_btn.text(),'为 2 家保存同一份需求')
        editor.save_btn.click()
        self.assertEqual(editor.result(),QDialog.DialogCode.Accepted)

        first, second = self.data()
        self.assertEqual([r['store'] for r in (first,second)],['B','A'])
        self.assertNotEqual(first['id'],second['id'])
        self.assertEqual(first['lines'],second['lines'])
        self.assertEqual([(r['sku'],r['quantity']) for r in first['lines']],[(SKU,'5'),('@ZERO','2')])
        revision = RequestEditor(self.parent,original=first)
        self.assertFalse(revision.warehouse.isEnabled())
        self.assertEqual(revision.stores_to_save(),('B',))
        revision.lines.item(0,1).setText('4')
        revision.save_btn.click()
        self.assertEqual([r['version'] for r in self.data()],[2,1])
        self.assertEqual(self.data()[1],second)

    def test_changing_warehouse_changes_choices_and_preserves_entered_draft(self):
        catalog(self.folder.name,HB,145,postfix='-other',sku='@HB-ONLY')
        editor = RequestEditor(self.parent,preset_warehouse=SD)
        self.enter(editor.products.search,SKU)
        editor.lines.item(0,1).setText('3')
        editor.products.items['@ZERO'].setCheckState(Qt.CheckState.Checked)
        editor.warehouse.setCurrentText(HB)
        self.assertEqual(set(editor.products.items),{'@HB-ONLY','@ZERO'})
        self.assertFalse(editor.products.checked)
        self.assertEqual(list(editor.draft_rows()),[dict(sku=SKU,quantity='3',note='')])

    def test_batch_rolls_back_when_later_store_is_missing(self):
        with self.assertRaisesRegex(ValueError,'未找到门店编码 REMOVED'):
            request_edit(self.parent.path,('A','REMOVED'),SD,[dict(sku=SKU,quantity='5')],{},
                         progress=lambda _: None,cancel=None)
        self.assertEqual(self.data(),[])
