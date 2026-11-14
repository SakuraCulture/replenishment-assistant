"""Real preview confirmation; zero additions cannot authorize a handoff."""
import os,unittest
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication,QDialog,QPushButton,QTableWidget,QAbstractItemView
from order_merge_ui import review


class MergeReview(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def plan(self,added):
        return {'package':'synthetic.zip','backup':'synthetic-backup','merge_report':{
            'counts':{'added':added,'duplicate':1,'conflict':1,'deleted':1},
            'orders':[{'day':'2026-11-13','store':'001','name':'合成门店','number':'BH-SYNTHETIC','status':status,'reason':'完整原因-'+status} for status in ('ADD','DUPLICATE','CONFLICT','DELETED')]}}
    def drive(self,added,accept):
        checked=[]
        def click():
            dialog=self.app.activeModalWidget();self.assertIsInstance(dialog,QDialog)
            table=dialog.findChild(QTableWidget,'orderMergePreview');self.assertEqual(table.rowCount(),4)
            self.assertEqual(table.editTriggers(),QAbstractItemView.EditTrigger.NoEditTriggers)
            confirm=dialog.findChild(QPushButton,'confirmOrderMerge');self.assertEqual(confirm.isEnabled(),bool(added))
            checked.append(True)
            dialog.findChild(QPushButton,'confirmOrderMerge' if accept else 'cancelOrderMerge').click()
        QTimer.singleShot(0,click)
        value=review(None,self.plan(added));self.assertTrue(checked);return value
    def test_cancel_never_accepts_scope(self):self.assertFalse(self.drive(1,False))
    def test_explicit_confirmation_accepts_only_additions(self):self.assertTrue(self.drive(1,True))
    def test_duplicate_and_conflict_only_cannot_confirm(self):self.assertFalse(self.drive(0,False))


if __name__=='__main__':unittest.main()
