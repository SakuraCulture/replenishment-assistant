"""Manual whole-order payment identity is independent of prior SKU routing."""
import os,sqlite3,unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch
import test_payment_matching as fixtures
import order_lifecycle as lc,payment_matching as matching


class BindingFixture:
    def setUp(self):self.f=fixtures.PaymentMatchingTests();self.f.setUp();self.addCleanup(self.f.tearDown)
    def document(self):return self.f.document(change=lambda row:row.update({'商品编码':'0002','商品金额':'123.45'}))


class PaymentBindingTests(BindingFixture,unittest.TestCase):
    def test_selected_whole_order_can_save_sku_that_used_to_belong_to_another_warehouse(self):
        f=self.f;document=self.document();index=matching.build(f.path)
        self.assertFalse(matching.analyze(f.path,document,index)['automatic'])
        document['target']=f.target;plan=matching.prepare(f.path,[document],None,index,None)
        raw=plan['plans'][0]['payload']['raw_rows'][0]
        self.assertEqual(raw['sku'],'0002');self.assertEqual(raw['matching_basis'],'清单外商品，需确认')
        self.assertEqual(plan['plans'][0]['head']['number'],f.target['number'])
        self.assertTrue(lc.commit(f.path,plan)[0]['changed'])
        saved=lc.snapshot(f.path,f.target)['documents']['payment']['payload']
        self.assertEqual(saved['raw_rows'][0]['sku'],'0002');self.assertEqual(saved['raw_rows'][0]['amount'],'123.45')
        self.assertTrue(next(r for r in saved['rows'] if r['sku']=='0002')['outside_basis'])


class PaymentBindingUiTests(BindingFixture,unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def dialog(self):
        from lifecycle_batch_ui import BatchImport
        f=self.f;env=patch.dict(os.environ,REPLENISHMENT_DATA_DIR=str(f.path.parent));env.start();self.addCleanup(env.stop)
        dialog=BatchImport(None,SimpleNamespace(work_repository=SimpleNamespace(path=f.path)),'payment');dialog.show()
        def close():dialog.close();dialog.deleteLater();self.app.processEvents()
        self.addCleanup(close);return dialog
    def test_button_enters_review_for_manually_selected_order_without_old_sku_warehouse_guard(self):
        from PySide6.QtWidgets import QMessageBox
        f=self.f;dialog=self.dialog();document=self.document()
        dialog.add_paths([document['scan']['files'][0]['path']]);self.assertTrue(dialog.read_ready,dialog.note.text())
        combo=dialog.targets[0];combo.setCurrentIndex(next(i for i in range(combo.count()) if combo.itemData(i) and combo.itemData(i)['number']==f.target['number']))
        with patch.object(QMessageBox,'exec',return_value=QMessageBox.StandardButton.Ok) as popup:dialog.preview_btn.click()
        self.assertIsNotNone(dialog.review);self.assertGreater(dialog.pages.currentIndex(),0);popup.assert_not_called()
        self.assertFalse(dialog.review.save.isEnabled());self.assertIsNone(lc.snapshot(f.path,f.target)['documents']['payment'])
    def test_unresolved_target_shows_popup_and_full_reason_without_losing_files_or_saving(self):
        from PySide6.QtWidgets import QMessageBox
        f=self.f;dialog=self.dialog();document=self.document();file=document['scan']['files'][0]['path']
        dialog.add_paths([file]);self.assertTrue(dialog.read_ready);self.assertIsNone(dialog.targets[0].currentData())
        with closing(sqlite3.connect(f.path)) as db:before='\n'.join(db.iterdump())
        boxes=[]
        with patch.object(QMessageBox,'exec',new=lambda box:boxes.append((box.windowTitle(),box.detailedText())) or QMessageBox.StandardButton.Ok):dialog.preview_btn.click()
        self.assertEqual(len(boxes),1);self.assertEqual(boxes[0][0],'尚未进入核对页');self.assertIn('没有完整匹配订单',boxes[0][1])
        self.assertEqual(dialog.pages.currentIndex(),0);self.assertEqual(dialog.paths,[file]);self.assertEqual(len(dialog.documents),1)
        self.assertTrue(dialog.preview_btn.isEnabled());self.assertFalse(dialog.busy)
        with closing(sqlite3.connect(f.path)) as db:self.assertEqual('\n'.join(db.iterdump()),before)
