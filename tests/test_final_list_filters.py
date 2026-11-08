import copy
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import unittest
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from test_preissue import row,result
import test_preissue_ui as existing_ui
import final_list_filters as f
from preissue_ui import FinalModel
from final_list_bulk_ui import plan,BulkDialog


class RuleTests(unittest.TestCase):
    def test_categories_or_cross_conditions_and_missing_not_zero(self):
        d=row();d.update(_category='饮料',U='快销',I=10,J=30,K=-2,L=0)
        spec={'categories':['饮料','食品'],'profiles':['快销'],'numbers':{'month':{'op':'ge','low':'30'},'week':{'op':'between','low':'5','high':'10'},'available':{'op':'le','low':'0'},'inbound':{'op':'eq','low':'0'}}}
        self.assertTrue(f.accepts(d,24,f.validate(spec)))
        self.assertFalse(f.accepts(dict(d,_category='日用'),24,spec))
        self.assertFalse(f.accepts(dict(d,J=None),24,spec))
        self.assertTrue(f.accepts(dict(d,J=None),24,{'numbers':{'month':{'op':'missing'}}}))
        self.assertFalse(f.accepts(dict(d,J=0),24,{'numbers':{'month':{'op':'missing'}}}))
    def test_missing_category_is_explicit(self):
        self.assertTrue(f.accepts(row(),24,{'categories':[None]}))
        self.assertFalse(f.accepts(dict(row(),_category='饮料'),24,{'categories':[None]}))
    def test_bad_intervals_and_non_finite_rejected(self):
        for low,high in [('3','2'),('','2'),('NaN','3'),('1','inf')]:
            with self.assertRaises(ValueError):f.validate({'numbers':{'month':{'op':'between','low':low,'high':high}}})
    def test_all_numeric_operators_and_holding_use_current_quantity(self):
        d=dict(row(),I=10,K=-2,L=3)
        for op,low,expected in [('gt','10',False),('ge','10',True),('lt','10',False),('le','10',True),('eq','10',True)]:
            self.assertEqual(f.accepts(d,24,{'numbers':{'week':{'op':op,'low':low}}}),expected)
        self.assertTrue(f.accepts(d,24,{'numbers':{'held':{'op':'eq','low':'27'}}}))
        self.assertFalse(f.accepts(d,48,{'numbers':{'held':{'op':'eq','low':'27'}}}))
    def test_pack_arithmetic_exact_and_no_negative_output(self):
        d=row()
        for mode,n,want in [('add',2,72),('subtract',2,0),('packs',3,72),('quantity',48,48),('zero',0,0)]:
            self.assertEqual(f.adjusted_quantity(d,24,mode,n),want)
        for mode,n in [('add',.5),('subtract',-1),('quantity',25)]:
            with self.assertRaises(ValueError):f.adjusted_quantity(d,24,mode,n)
    def test_plan_uses_each_pack_and_current_overlay_is_pure(self):
        r=result([row(),row('b',12,12,2)])
        m=FinalModel({'S1':r},{'S1':{'edits':{'a':{'quantity':48,'reason':'原调整'}}}},lambda *_:None)
        before=copy.deepcopy((m.edits,r));p=plan(m,[0,1],'add',1,'活动备货')
        self.assertEqual([x[5] for x in p['rows']],[72,24]);self.assertEqual(p['amount_delta'],'48')
        self.assertEqual((m.edits,r),before);self.assertEqual(p['edits']['S1']['a']['reason'],'活动备货')
    def test_invalid_reason_or_one_blocked_row_has_no_partial_edits(self):
        r=result([row(),row('b')]);m=FinalModel({'S1':r},{},lambda *_:None)
        with self.assertRaises(ValueError):plan(m,[0],'add',1,'')
        m.blocks[1]='资料待核'
        with self.assertRaisesRegex(ValueError,'资料待核'):plan(m,[0,1],'add',1,'经营判断')
        self.assertEqual(m.edits,{'S1':{}})


class FilterUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):existing_ui.FinalUiTests.setUpClass.__func__(cls)
    setUp=existing_ui.FinalUiTests.setUp
    tearDown=existing_ui.FinalUiTests.tearDown
    create=existing_ui.FinalUiTests.create
    wait_task=existing_ui.FinalUiTests.wait_task
    def test_combined_filter_keeps_hidden_checks_and_refreshes_held_after_edit(self):
        self.host.results['S1']=result([dict(row(),_category='饮料',I=10,J=30),dict(row('b',12,12,2),_category='食品',I=1,J=None)])
        d=self.create();d.check_visible(True)
        d.set_business_filters({'categories':['饮料'],'numbers':{'month':{'op':'ge','low':'30'}}})
        self.assertEqual(d.proxy.rowCount(),1);self.assertEqual(d.chosen(),[0]);self.assertIn('筛选外1款',d.metrics.text())
        d.selection_scope.setCurrentIndex(1);self.assertEqual(d.chosen(),[0,1])
        d.selection_scope.setCurrentIndex(0)
        d.set_business_filters({'numbers':{'held':{'op':'le','low':'24'}}});self.assertEqual(d.proxy.rowCount(),2)
        d.model.reason='经营判断';d.model.setData(d.model.index(0,9),48);self.app.processEvents();self.app.processEvents()
        self.assertEqual(d.proxy.rowCount(),1)
        d.reset_filters();self.assertEqual(d.proxy.rowCount(),2);self.assertEqual(len(d.model.checked),2)
    def test_filter_dialog_validation_cancel_and_all_category_values(self):
        from final_list_filter_ui import FilterDialog
        d=FilterDialog([dict(row(),_category='饮料'),row()],{})
        op,low,high=d.numbers['month'];op.setCurrentIndex(op.findData('between'));low.setText('5');high.setText('2');d.accept()
        self.assertIn('下限',d.error.text());high.setText('10');d.accept();self.assertEqual(d.spec['numbers']['month']['high'],'10')
        d.deleteLater()
    def test_batch_preview_cancel_and_changed_input_require_repreview(self):
        d=self.create();before=copy.deepcopy(d.model.edits);b=BulkDialog(d.model,[0,1],'当前可见勾选',d)
        b.preview();self.assertIsNone(b.prepared);self.assertFalse(b.fill.isEnabled())
        b.reason.setText('活动备货');b.preview();self.assertEqual(b.table.model().rowCount(),2);self.assertTrue(b.fill.isEnabled())
        b.amount.setText('2');self.assertIsNone(b.prepared);self.assertFalse(b.fill.isEnabled());b.reject()
        self.assertEqual(d.model.edits,before);b.deleteLater()


class ExportBlockTests(unittest.TestCase):
    def test_slim_export_preflight_preserves_selected_pending_adjustments(self):
        import normal_delivery,preissue,work_repository
        from version_info import MODEL_VERSION
        full={'schema':1,'day':work_repository.today(),'input_dirty':False,'new_focus':None,'model_version':MODEL_VERSION,
              'states':{'S1':{'state':'已完成','name':'合成店'}},'results':{'S1':result()},'preissue_drafts':{'S1':{'status':'待核对'},'unselected':{}}}
        slim=normal_delivery.probe_payload(full,['S1'])
        self.assertEqual(slim['preissue_drafts'],{'S1':{'status':'待核对'}})
        with self.assertRaisesRegex(ValueError,'待核对'):preissue.validate_payload(slim,['S1'])
        del full['new_focus']
        self.assertIsNone(normal_delivery.probe_payload(full,['S1'])['new_focus'])
    def test_pending_export_error_does_not_advise_file_occupancy(self):
        from readable_ui import failure_message
        text=failure_message('导出任务与恢复\n已生成 0/4 份\n门店尚有待核对的签发前调整：S1')
        self.assertIn('应用最终数量',text);self.assertIn('S1',text);self.assertNotIn('关闭占用文件',text)
