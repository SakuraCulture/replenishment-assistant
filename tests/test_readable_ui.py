import os,sys,unittest,copy
from pathlib import Path
from types import SimpleNamespace
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from PySide6.QtWidgets import QApplication,QWidget,QMessageBox
from PySide6.QtCore import QTimer
import parameter_ui,parameter_schema,readable_ui

class ReadableUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def test_percentage_editor_keeps_fraction_and_accepts_percent_text(self):
        w=parameter_ui.PercentInput();w.setDecimals(8);w.setRange(.5,.99);w.setValue(.905)
        self.assertEqual(w.value(),.905);self.assertEqual(w.textFromValue(.905),'90.5')
        self.assertEqual(w.valueFromText('95%'),.95)
        w.lineEdit().setText('95');w.interpretText();self.assertEqual(w.value(),.95)
    def test_parameter_dialog_preserves_values_and_splits_warehouse_pairs(self):
        c=parameter_schema.standard();c['warehouses']={k:list(v) for k,v in c['warehouses'].items()};c['profile_parameters']={'快销':{'probability':.95}}
        p={'id':'standard','name':'测试','version':1,'builtin':True,'config':copy.deepcopy(c)}
        h=QWidget();h.work_repository=SimpleNamespace(presets=lambda:[p])
        d=parameter_ui.ParameterDialog(h,c,p,scope='所选1家门店')
        self.assertEqual(d.value(),c)
        i=list(parameter_schema.HELP).index('probability');self.assertNotIn('probability',d.param_table.item(i,0).text())
        texts=[d.effective.item(i,0).text() for i in range(d.effective.rowCount())]
        self.assertTrue(any('河北仓 / 备货天数' in x for x in texts));self.assertFalse(any('[' in d.effective.item(i,4).text() for i in range(d.effective.rowCount())))
        d.deleteLater();h.deleteLater();self.app.processEvents()
    def test_review_keeps_cancel_default_and_all_ordered_rows(self):
        box=QMessageBox();readable_ui.review_table(box,['商品','原量','新量'],[['A',5,0],['B',0,6]])
        cancel=box.addButton('取消',QMessageBox.ButtonRole.RejectRole);ok=box.addButton('确认',QMessageBox.ButtonRole.AcceptRole);box.setDefaultButton(cancel)
        QTimer.singleShot(20,cancel.click);box.exec()
        self.assertIs(box.clickedButton(),cancel);self.assertEqual(box.business_model.rows,[['A',5,0],['B',0,6]])
        box.business_search.setText('B');self.assertEqual(box.business_model.indices,[1]);self.assertEqual(box.business_model.rows[0][0],'A')
        box.deleteLater();self.app.processEvents()

if __name__=='__main__':unittest.main()
