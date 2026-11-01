import copy
import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from test_preissue import result,row,edit,listing,commit


@unittest.skipUnless(importlib.util.find_spec('PySide6'),'optional Qt runtime')
class FinalUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        from PySide6.QtWidgets import QWidget
        import work_repository as wr
        self.tmp=tempfile.TemporaryDirectory();self.host=QWidget();self.host.work_repository=wr.Repository(Path(self.tmp.name))
        self.host.results={'S1':result()};self.host.states={'S1':{'state':'已完成','name':'合成店'}};self.host.preissue_drafts={}
        self.host.readonly=False;self.host.input_dirty=False;self.host.job_day=wr.today();self.dialog=None
    def tearDown(self):
        import shiboken6
        if self.dialog:
            self.wait_task();self.dialog.dirty=False;self.dialog.reject();shiboken6.delete(self.dialog)
        shiboken6.delete(self.host);self.tmp.cleanup()
    def create(self):
        from preissue_ui import FinalListDialog
        self.dialog=FinalListDialog(self.host,['S1']);return self.dialog
    def wait_task(self):
        deadline=time.monotonic()+10
        while self.dialog and self.dialog.task and time.monotonic()<deadline:
            self.app.processEvents();time.sleep(.005)
        self.app.processEvents()
        if self.dialog:self.assertIsNone(self.dialog.task)
    def test_pack_editor_arrows_and_manual_invalid(self):
        from preissue_ui import PackInput
        import shiboken6
        x=PackInput(24);x.stepBy(1);self.assertEqual(x.value(),24);x.stepBy(1);self.assertEqual(x.value(),48)
        x.stepBy(-3);self.assertEqual(x.value(),0);x.lineEdit().setText('25')
        with self.assertRaises(ValueError):x.value()
        x.stepBy(1);self.assertEqual(x.lineEdit().text(),'25');shiboken6.delete(x)
    def test_invalid_keyboard_does_not_change_quantity(self):
        d=self.create();m=d.model;before=m.qty(0)
        self.assertFalse(m.setData(m.index(0,9),'25'));self.assertEqual(m.qty(0),before);self.assertTrue(m.invalid)
        d.preview();self.assertIsNone(d.task);self.assertIsNone(d.prepared)
    def test_sort_mapping_keeps_identity(self):
        from PySide6.QtCore import Qt
        d=self.create();d.model.reason='经营判断';d.proxy.sort(3,Qt.SortOrder.DescendingOrder)
        self.assertEqual(d.proxy.index(0,3).data(),'b')
        self.assertTrue(d.proxy.setData(d.proxy.index(0,9),24))
        self.assertNotIn('a',d.model.edits['S1']);self.assertEqual(d.model.edits['S1']['b']['quantity'],24)
    def test_filter_hidden_selection_stays(self):
        d=self.create();d.check_visible(True);d.search.setText('合成商品a');d.filter()
        self.assertEqual(d.proxy.rowCount(),1);self.assertEqual(len(d.model.checked),2);self.assertIn('筛选外1',d.metrics.text())
    def test_zero_risk_requires_checkbox(self):
        d=self.create();d.model.reason='暂不补货';d.model.setData(d.model.index(0,9),0);d.preview();self.wait_task()
        self.assertIsNotNone(d.prepared);self.assertFalse(d.apply_btn.isEnabled());self.assertGreater(d.review.rowCount(),0)
        d.confirm_all.setChecked(True);self.assertTrue(d.apply_btn.isEnabled())
        d.apply();self.wait_task();self.assertEqual(d.outcome['action'],'apply');self.assertEqual(d.outcome['results']['S1']['rows'][0]['_execution_qty'],0)
        self.assertEqual(self.host.results['S1']['rows'][0]['AS'],24)
    def test_edit_invalidates_previous_preview(self):
        d=self.create();d.model.reason='经营判断';d.model.setData(d.model.index(0,9),48);d.preview();self.wait_task();self.assertIsNotNone(d.prepared)
        d.model.setData(d.model.index(0,9),72);self.assertIsNone(d.prepared);self.assertFalse(d.apply_btn.isEnabled())
        self.assertEqual(d.model.risks,{});self.assertFalse(d.model.reviewed)
    def test_confirmed_risk_leaves_pending_filter_and_edit_clears_it(self):
        d=self.create();d.model.reason='暂不补货';d.model.setData(d.model.index(0,9),0);d.preview();self.wait_task()
        d.scope.setCurrentText('风险待确认');self.app.processEvents();self.assertEqual(d.proxy.rowCount(),1)
        d.confirm_all.setChecked(True);self.app.processEvents();self.assertEqual(d.proxy.rowCount(),0)
        d.model.setData(d.model.index(0,9),24);self.app.processEvents()
        self.assertIsNone(d.prepared);self.assertFalse(d.model.risks);self.assertFalse(d.apply_btn.isEnabled())
    def test_check_and_reason_updates_are_data_changes_not_unpaired_layout(self):
        from PySide6.QtCore import QPersistentModelIndex,Qt
        d=self.create();index=QPersistentModelIndex(d.proxy.index(0,3));sku=index.data();signals=[]
        d.model.layoutChanged.connect(lambda *args:signals.append('layout'))
        d.check_visible(True);d.model.reason='经营判断';d.model.setData(d.model.index(0,9),48);d.assign_reason();d.clear_checks();self.app.processEvents()
        self.assertEqual(signals,[]);self.assertTrue(index.isValid());self.assertEqual(index.data(),sku)
    def test_actual_delegate_edit_commit_keeps_sku_under_sort_and_filter(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest,QAbstractItemModelTester
        from preissue_ui import PackInput
        self.host.results['S1']=result([row(str(i),24,24,2) for i in range(30)])
        d=self.create();d.show();d.model.reason='暂不补货'
        testers=[QAbstractItemModelTester(m,QAbstractItemModelTester.FailureReportingMode.Fatal) for m in (d.model,d.proxy)]
        d.scope.setCurrentText('拟签发大于0');d.table.sortByColumn(9,Qt.SortOrder.DescendingOrder)
        for n in range(20):
            self.app.processEvents();target=d.proxy.index(0,3).data();index=d.proxy.index(0,9)
            d.table.selectRow(0);d.table.setCurrentIndex(index);d.table.edit(index);self.app.processEvents()
            editor=d.table.findChild(PackInput);self.assertIsNotNone(editor)
            editor.lineEdit().selectAll();QTest.keyClicks(editor.lineEdit(),'0');QTest.keyClick(editor.lineEdit(),Qt.Key.Key_Return)
            self.app.processEvents();d.check_visible(True);d.table.viewport().repaint();d.table.horizontalHeader().repaint();self.app.processEvents()
            self.assertEqual(d.model.edits['S1'][target]['quantity'],0)
            self.assertEqual(d.proxy.rowCount(),29-n)
        self.assertEqual(len(d.model.edits['S1']),20)
    def test_draft_not_applied_and_keeps_zero(self):
        d=self.create();d.model.reason='暂不补货';d.model.setData(d.model.index(0,9),0);d.keep_draft()
        self.assertEqual(d.outcome['drafts']['S1']['edits']['a']['quantity'],0);self.assertEqual(self.host.results['S1']['rows'][0]['AS'],24)
    def test_other_window_job_change_rejects(self):
        d=self.create();h=self.host.work_repository
        from version_info import MODEL_VERSION
        p={'schema':1,'day':self.host.job_day,'model_version':MODEL_VERSION,'results':self.host.results,'states':self.host.states,'input_dirty':False}
        h.save_job(self.host.job_day,p)
        with self.assertRaisesRegex(ValueError,'其他窗口'):d.current_guard()
    def test_result_change_rejects(self):
        d=self.create();self.host.results['S1']=copy.deepcopy(self.host.results['S1'])
        with self.assertRaisesRegex(ValueError,'变化'):d.current_guard()
    def test_multi_store_finalize_atomic(self):
        from preissue_ui import preview_batch,finalize_batch
        import preissue as p
        results=self.host.results;v=preview_batch(results,{'S1':{'a':edit(0)}},{'S1':{}},self.host.job_day)
        with self.assertRaises(ValueError):finalize_batch(v,[],results)
        self.assertNotIn('preissue',results['S1'])
    def test_partial_restore_does_not_clear_another_invalid_input(self):
        d=self.create();d.model.setData(d.model.index(0,9),'25');d.model.setData(d.model.index(1,9),'13')
        d.model.replace_edits({'S1':{}},cleared=[('S1','a')])
        self.assertEqual(set(d.model.invalid),{('S1','b')});d.preview();self.assertIsNone(d.task)


if __name__=='__main__':unittest.main()
