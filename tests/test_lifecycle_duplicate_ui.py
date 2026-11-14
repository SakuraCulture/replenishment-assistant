"""Real worker previews with explicit duplicate choices and isolated databases."""
import copy
import hashlib
import os
import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import test_lifecycle as fixture
import lifecycle_batch as batch
import lifecycle_duplicates as duplicates
import order_lifecycle as service
from PySide6.QtWidgets import QApplication,QDialog,QMessageBox
from lifecycle_batch_ui import BatchImport
from lifecycle_duplicate_ui import DuplicateResolution


class LifecycleDuplicateUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.f=fixture.LifecycleTests();self.f.setUp()
        self.env=patch.dict(os.environ,{
            'REPLENISHMENT_DATA_DIR':str(self.f.root/'private-settings'),
            'LOCALAPPDATA':str(self.f.root/'local'),
            'APPDATA':str(self.f.root/'roaming'),
        });self.env.start();self.windows=[];self.unexpected_popups=[]
        self.popup_guard=patch.object(QMessageBox,'exec',new=lambda box:self.unexpected_popups.append(box.detailedText()) or QMessageBox.StandardButton.Ok)
        self.popup_guard.start()

    def tearDown(self):
        for window in reversed(self.windows):window.close();window.deleteLater()
        self.app.processEvents();self.popup_guard.stop();self.env.stop();self.f.tearDown()
        self.assertEqual(self.unexpected_popups,[])

    def document(self,two=False):
        items=[{'门店确认数量':0},{'门店确认数量':7}]
        if two:items.extend([{'商品编码':'other','门店确认数量':0},{'商品编码':'other','门店确认数量':3}])
        document=batch.group_scan(self.f.scan(items=items))[0]
        document['target']=self.f.h
        return document

    def resolver(self,document):
        dialog=DuplicateResolution(None,document);self.windows.append(dialog);return dialog

    def batch_dialog(self):
        host=SimpleNamespace(work_repository=SimpleNamespace(path=self.f.path),closing=False)
        dialog=BatchImport(None,host,'return',scope=self.f.heads)
        self.windows.append(dialog);dialog.show();return dialog

    def dump(self):
        with closing(sqlite3.connect(self.f.path)) as db:return '\n'.join(db.iterdump())

    def test_no_default_and_every_conflict_requires_an_explicit_choice(self):
        document=self.document(two=True);original=copy.deepcopy(document);dialog=self.resolver(document)
        self.assertEqual([c.currentIndex() for c in dialog.choices],[0,0])
        self.assertFalse(dialog.apply_btn.isEnabled());dialog.finish();self.assertIsNone(dialog.resolved)
        dialog.choices[0].setCurrentIndex(2);self.assertFalse(dialog.apply_btn.isEnabled())
        dialog.choices[1].setCurrentIndex(1);self.assertTrue(dialog.apply_btn.isEnabled())
        dialog.finish();self.assertEqual(dialog.result(),QDialog.DialogCode.Accepted)
        self.assertEqual([r['quantity'] for r in dialog.resolved['scan']['rows']],['7','0'])
        self.assertEqual(document,original)

    def test_zero_and_positive_are_equal_choices_and_details_show_source_rows(self):
        for index,quantity in ((1,'0'),(2,'7')):
            with self.subTest(quantity=quantity):
                document=self.document();dialog=self.resolver(document)
                dialog.choices[0].setCurrentIndex(index)
                text=dialog.details.toPlainText()
                self.assertIn('业务明细',text);self.assertIn('第2行',text);self.assertIn('第3行',text)
                dialog.finish();row=dialog.resolved['scan']['rows'][0]
                self.assertEqual(row['quantity'],quantity)
                self.assertEqual(row['duplicate_resolution']['selected_source']['row'],index+1)

    def test_cancel_preserves_originals_and_does_not_start_prepare_or_save(self):
        document=self.document();path=Path(document['scan']['files'][0]['path']);before=hashlib.sha256(path.read_bytes()).hexdigest()
        dialog=self.batch_dialog();dialog.add_paths([str(path)])
        self.assertTrue(dialog.read_ready,dialog.note.text());dialog.capture();original=copy.deepcopy(dialog.documents);before_db=self.dump()
        with patch('lifecycle_duplicate_ui.resolve_document',return_value=None),patch('lifecycle_batch.prepare') as prepare:
            dialog.preview_btn.click()
        prepare.assert_not_called();self.assertIsNone(dialog.review);self.assertFalse(dialog.busy)
        self.assertEqual(dialog.documents,original);self.assertIn('已取消',dialog.note.text())
        self.assertEqual(self.dump(),before_db);self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)

    def test_native_modal_choice_returns_only_after_explicit_acceptance(self):
        from PySide6.QtCore import QTimer
        from lifecycle_duplicate_ui import resolve_document
        document=self.document();seen=[]
        def choose():
            dialog=self.app.activeModalWidget()
            if isinstance(dialog,DuplicateResolution):
                seen.append(dialog.objectName());dialog.choices[0].setCurrentIndex(2);dialog.apply_btn.click()
            elif dialog:dialog.reject()
        QTimer.singleShot(0,choose)
        resolved=resolve_document(None,document)
        self.assertEqual(seen,['lifecycleDuplicateResolution'])
        self.assertEqual(resolved['scan']['rows'][0]['quantity'],'7')

    def test_later_document_cancel_discards_earlier_choices_without_partial_preview(self):
        first=self.document();head=self.f.heads[1]
        second=batch.group_scan(self.f.scan(items=[{'商品编码':'0002','补货单号':head['number'],'供货仓':head['warehouse'],'门店确认数量':quantity} for quantity in (0,7)]))[0]
        dialog=self.batch_dialog();dialog.add_paths([first['scan']['files'][0]['path'],second['scan']['files'][0]['path']]);dialog.capture()
        original=copy.deepcopy(dialog.documents);before=self.dump()
        conflict=duplicates.find(dialog.documents[0]['scan']['rows'])[0]
        resolved=duplicates.resolve(dialog.documents[0],{conflict['token']:duplicates.candidate_id(conflict['rows'][1])})
        with patch('lifecycle_duplicate_ui.resolve_document',side_effect=[resolved,None]),patch('lifecycle_batch.prepare') as prepare:
            dialog.preview_btn.click()
        prepare.assert_not_called();self.assertIsNone(dialog.review)
        self.assertEqual(dialog.documents,original);self.assertEqual(self.dump(),before)

    def choose_exec(self,quantity):
        def choose(dialog):
            for conflict,combo in zip(dialog.conflicts,dialog.choices):
                row=next(r for r in conflict['rows'] if r['quantity']==quantity)
                wanted=duplicates.candidate_id(row)
                combo.setCurrentIndex(next(i for i in range(combo.count()) if combo.itemData(i)==wanted))
            dialog.apply_btn.click()
            return dialog.result()
        return choose

    def test_real_worker_preview_save_and_repeat_retain_selected_row_audit(self):
        document=self.document();path=Path(document['scan']['files'][0]['path']);original_hash=hashlib.sha256(path.read_bytes()).hexdigest()
        dialog=self.batch_dialog();dialog.add_paths([str(path)])
        with patch.object(DuplicateResolution,'exec',new=self.choose_exec('0')):
            dialog.preview_btn.click()
        self.assertIsNotNone(dialog.review,dialog.note.text());self.assertGreater(dialog.pages.currentIndex(),0)
        row=dialog.plan['plans'][0]['payload']['raw_rows'][0]
        self.assertEqual(row['quantity'],'0');self.assertEqual(len(row['duplicate_resolution']['candidates']),2)
        self.assertEqual(len(dialog.documents[0]['scan']['rows']),2)
        self.assertFalse(dialog.review.save.isEnabled())
        dialog.review.complete.setChecked(True);dialog.review.extra_ack.setChecked(True)
        dialog.review.save.click();self.assertTrue(dialog.receipt)
        saved=service.snapshot(self.f.path,self.f.h)['documents']['return']['payload']['raw_rows'][0]
        self.assertEqual(saved['quantity'],'0');self.assertEqual(saved['duplicate_resolution'],row['duplicate_resolution'])
        second=self.batch_dialog();second.add_paths([str(path)])
        with patch.object(DuplicateResolution,'exec',new=self.choose_exec('0')):second.preview_btn.click()
        self.assertFalse(second.plan['plans'][0]['changed'])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),original_hash)

    def test_return_to_files_requires_fresh_choice_and_keeps_original_rows(self):
        document=self.document();dialog=self.batch_dialog();dialog.add_paths([document['scan']['files'][0]['path']])
        with patch.object(DuplicateResolution,'exec',new=self.choose_exec('7')):dialog.preview_btn.click()
        self.assertEqual(dialog.plan['plans'][0]['payload']['raw_rows'][0]['quantity'],'7')
        dialog.pages.setCurrentIndex(0)
        with patch('lifecycle_duplicate_ui.resolve_document',return_value=None) as resolver:dialog.preview_btn.click()
        resolver.assert_called_once();self.assertEqual(len(dialog.documents[0]['scan']['rows']),2)
        self.assertEqual(dialog.pages.currentIndex(),0);self.assertIn('已取消',dialog.note.text())
        self.assertIsNone(service.snapshot(self.f.path,self.f.h)['documents']['return'])

    def test_unselected_conflict_does_not_prompt_or_affect_other_order(self):
        bad=self.document();good=batch.group_scan(self.f.scan(items=[{'商品编码':'0002','补货单号':self.f.heads[1]['number'],'供货仓':'山东仓'}]))[0]
        dialog=self.batch_dialog();dialog.add_paths([bad['scan']['files'][0]['path'],good['scan']['files'][0]['path']])
        from PySide6.QtCore import Qt
        dialog.table.item(0,0).setCheckState(Qt.CheckState.Unchecked)
        with patch('lifecycle_duplicate_ui.resolve_document') as resolver:dialog.preview_btn.click()
        resolver.assert_not_called();self.assertIsNotNone(dialog.review,dialog.note.text())
        self.assertEqual([p['head']['warehouse'] for p in dialog.plan['plans']],['山东仓'])

    def test_real_worker_error_uses_failing_document_not_current_selection(self):
        from business_ui import run
        bad=self.document();bad_path=Path(bad['scan']['files'][0]['path']);bad_file=bad_path.with_name('河东冲突.xlsx');bad_path.rename(bad_file)
        good_scan=self.f.scan(items=[{'商品编码':'0002','补货单号':self.f.heads[1]['number'],'供货仓':'山东仓'}]);good_path=Path(good_scan['files'][0]['path']);good_file=good_path.with_name('定陶正常.xlsx');good_path.rename(good_file)
        dialog=self.batch_dialog();dialog.add_paths([str(good_file),str(bad_file)])
        self.assertEqual(dialog.table.currentRow(),0);before=self.dump()
        with self.assertRaises(RuntimeError) as error:
            run(dialog,batch.prepare,(self.f.path,dialog.documents,self.f.heads),'隔离冲突定位验收')
        text=str(error.exception);self.assertIn('河东冲突.xlsx',text);self.assertIn('第2行',text);self.assertIn('第3行',text)
        boxes=[]
        with patch.object(QMessageBox,'exec',new=lambda box:boxes.append(box.detailedText()) or QMessageBox.StandardButton.Ok):
            dialog.problem('尚未进入核对页',text)
        self.assertEqual(len(boxes),1);self.assertNotIn('定陶正常.xlsx',boxes[0]);self.assertIn('河东冲突.xlsx',dialog.selection_note.toPlainText())
        self.assertEqual(self.dump(),before)


if __name__=='__main__':unittest.main()
