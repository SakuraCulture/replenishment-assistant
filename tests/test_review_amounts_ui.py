import copy
import importlib.util
import os
import tempfile
import unittest
from unittest.mock import patch
from test_review_amounts import new_row
from test_preissue import row,result,commit,edit
import new_products

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')


@unittest.skipUnless(importlib.util.find_spec('PySide6'),'optional Qt runtime')
class ReviewMoneyUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,REPLENISHMENT_DATA_DIR=self.tmp.name);self.env.start()
        from application_window import ApplicationWindow
        self.win=ApplicationWindow();w=self.win
        r=new_products.apply(result([row(),new_row()]),{'new':3})
        self.original=copy.deepcopy(r);r=commit(r,{'a':edit(0),'new':edit(5)})
        other=dict(result([row('other')]),store='S2',store_name='另一店')
        w.results={'S1':r,'S2':other};w.states={s:{'state':'已完成','name':x['store_name'],'error':''} for s,x in w.results.items()}
        w.loaded={'stores':{s:x['store_name'] for s,x in w.results.items()},'intake':{'conflicts':[]}};w.current_store='S1';w.result=r;w.dirty=False
        w.refresh_tables()
    def tearDown(self):
        import shiboken6
        self.win.stop_filtered_summary();shiboken6.delete(self.win);self.env.stop();self.tmp.cleanup()
    def test_summary_click_filters_zero_and_overlap(self):
        w=self.win;before=copy.deepcopy(w.results)
        self.assertEqual(w.review_amounts.values['manual'].text(),'10.00')
        self.assertIn('-20.00',w.review_amounts.details['manual'].text())
        self.assertEqual(w.review_amounts.values['new'].text(),'10.00')
        self.assertEqual(w.overview.horizontalHeader().visualIndex(12),5)
        self.assertEqual(w.overview.horizontalHeader().visualIndex(13),6)
        i=w.overview_codes.index('S1');w.open_overview(i,12)
        self.assertEqual(w.mode.currentText(),'人工改量商品')
        self.assertEqual({d['E'] for d in w.visible_rows()},{'a','new'})
        self.assertEqual(w.detail_review_amounts.values['new'].text(),'10.00')
        w.open_overview(i,13);self.assertEqual([d['E'] for d in w.visible_rows()],['new'])
        w.open_overview(i,7);self.assertEqual(w.mode.currentText(),'预算未满足')
        self.assertEqual(w.results,before)
    def test_stale_pending_draft_and_restore(self):
        w=self.win;w.preissue_drafts={'S1':{'edits':{'a':{'quantity':2400}}}};w.refresh_tables()
        self.assertEqual(w.review_amounts.values['manual'].text(),'10.00')
        self.assertIn('待核对',w.overview.item(w.overview_codes.index('S1'),11).text())
        w.states['S1']['state']='待重算';w.refresh_tables()
        self.assertEqual(w.review_amounts.values['manual'].text(),'0.00')
        self.assertEqual(w.overview.item(w.overview_codes.index('S1'),12).text(),'—')
        w.states['S2']['state']='计算失败';w.refresh_tables()
        self.assertEqual(w.review_amounts.values['new'].text(),'—')
        w.results['S1']=copy.deepcopy(self.original);w.states['S1']['state']='已完成';w.preissue_drafts={};w.refresh_tables()
        self.assertEqual(w.review_amounts.values['manual'].text(),'0.00')
        self.assertEqual(w.review_amounts.values['new'].text(),'6.00')
    def test_detail_is_store_scope_search_does_not_recount(self):
        w=self.win;w.open_overview(w.overview_codes.index('S1'),12);w.search.setText('nonexistent');w.filter_changed()
        self.assertEqual(w.proxy.rowCount(),0)
        self.assertEqual(w.detail_review_amounts.values['manual'].text(),'10.00')
        w.set_rows(w.result['rows'],False)
        self.assertEqual(w.detail_review_amounts.values['manual'].text(),'—')
    def test_excluded_view_is_explicit_read_only_and_work_scope_unchanged(self):
        from ui_goods_fast import FilterModel
        w=self.win;d=dict(row(),S='禁用');p=FilterModel();p.mode='已排除'
        self.assertFalse(p.accepts(d));p.include_excluded=True;self.assertTrue(p.accepts(d))
        p.mode='补货建议停采';self.assertFalse(p.accepts(d))
        stopped=dict(row(),_meta={'notes':[],'blocking':[],'export_status':'停采'});self.assertTrue(p.accepts(stopped))
        self.assertEqual(w.mode.findText('已排除'),-1)
    def test_new_selection_combines_month_selection_budget_and_barcode(self):
        from new_product_ui import NewProductDialog
        import new_product_focus as focus
        import shiboken6
        drow=dict(new_row(),F='001234',_budget_active=True,_allocated_qty=1,_allocated_amount=2,_budget_gap=2)
        r=result([drow]);d=NewProductDialog(self.win,r,r,focus.empty('2026-11'))
        try:
            d.group.setCurrentText('往期新品');d.budget_filter.setCurrentText('部分安排');d.search.setText('001234');d.refresh();self.assertEqual(d.model.rowCount(),1)
            d.budget_filter.setCurrentText('预算暂缓');self.assertEqual(d.model.rowCount(),0)
            d.reset_filters();self.assertEqual(d.model.rowCount(),1)
        finally:shiboken6.delete(d)


if __name__=='__main__':unittest.main()
