import os
import unittest
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication,QMessageBox,QPlainTextEdit
from PySide6.QtCore import QTimer,Qt
from PySide6.QtTest import QTest
import readable_ui
import display_settings
from table_readability import row_values


class ReadingWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def tearDown(self):display_settings.apply({})
    def test_resizable_review_preserves_cancel_scope_and_full_text(self):
        rows=[['S1','A','长原因'*800],['S2','B','未选店原值']]
        d=readable_ui.ReviewDialog();readable_ui.review_table(d,['门店','商品','原因'],rows)
        cancel=d.addButton('取消',QMessageBox.ButtonRole.RejectRole);accept=d.addButton('确认',QMessageBox.ButtonRole.AcceptRole)
        d.setDefaultButton(cancel);d.setEscapeButton(cancel)
        d.business_search.setText('S1');self.assertEqual(d.business_model.rowCount(),1)
        d.business_table.setCurrentIndex(d.business_model.index(0,0))
        self.assertEqual(row_values(d.business_table)[1][2],rows[0][2])
        self.assertEqual(d.business_model.rows,rows)
        QTimer.singleShot(20,d.reject);d.exec()
        self.assertIs(d.clickedButton(),cancel);self.assertIs(d.defaultButton(),cancel);self.assertIs(d.escapeButton(),cancel)
        self.assertGreaterEqual(d.maximumWidth(),d.width());d.deleteLater();self.app.processEvents()
    def test_font_spacing_changes_only_display_and_corrupt_width_settings_are_ignored(self):
        from business_ui import grid
        d=readable_ui.ReviewDialog();table,model=grid(d,['门店编码','商品名称','风险原因'],[['S','商品','完整原因']]);d.body.addWidget(table);d.show()
        table.setCurrentIndex(model.index(0,1));QTest.qWait(40)
        saved=getattr(self.app,'reading_settings',None)
        try:
            self.app.reading_settings={'tables':{table.reading_controller.key:{'wrong':'type'}}}
            table.reading_controller.headers=None
            display_settings.apply({'font':12,'density':'roomy'});QTest.qWait(50)
            self.assertGreaterEqual(table.verticalHeader().defaultSectionSize(),table.fontMetrics().height()+8)
            self.assertEqual(model.rows,[['S','商品','完整原因']]);self.assertEqual(table.currentIndex().row(),0)
        finally:
            self.app.reading_settings=saved or {};d.close();d.deleteLater();self.app.processEvents()
    def test_export_failure_keeps_business_state_in_visible_message(self):
        raw='导出操作未全部完成：所选正常单已归档，修订保留。\n已生成 1/3 份：D:\\long-path\\a.xlsx\n未生成：D:\\other.xlsx\n可到“订单跟踪 → 导出任务与恢复”查看逐文件结果。'
        message=readable_ui.failure_message(raw)
        for fragment in ('已归档','1/3','导出任务与恢复','不要重新签发'):self.assertIn(fragment,message)
        self.assertNotIn('D:\\',message)
    def test_prompt_preserves_exact_scope_and_never_promises_autoconfiguration(self):
        from stockout_ai_ui import prompt
        query={'start':'2026-11-01','end':'2026-11-07','filters':{'store':['精确S2','精确S1']},'exclude_stopped':True}
        value=prompt(query,['store','supplier'],'为后续调参准备证据')
        self.assertLess(value.index('精确S2'),value.index('精确S1'))
        for fragment in ('不自动采用或保存默认','query_order_funnel','没有精确原单关联','stockout_analysis_results'):self.assertIn(fragment,value)
