"""Selected original-batch routing from the date view; no platform access."""
import copy
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import test_jst as fixture
import jst_batches as batches
import jst_orders as orders
import order_lifecycle as lifecycle
import jst_results as results


class PaidReturnUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.f=fixture.Service();self.f.setUp();self.path=self.f.path;self.h=self.f.h
        self.env=patch.dict(os.environ,REPLENISHMENT_DATA_DIR=str(Path(self.path).parent));self.env.start()
        self.reader=patch('history_tasks.HistoryReader.request');self.reader.start()
        self.host=SimpleNamespace(work_repository=SimpleNamespace(path=self.path),repository=SimpleNamespace(folder=Path(self.path).parent),closing=False)
        r=batches.execute(self.path,batches.preview_new(self.path,[self.h],{'河北仓':fixture.catalog()}));self.bid=r['batch_id'];self.old=r['generated'][0]
        lifecycle.commit(self.path,orders.payment_plan(self.path,self.old));self.f.f.save('return',[{'门店确认数量':30}])
        from jst_results_ui import ResultsPage
        self.page=ResultsPage(self.host,[self.h]);self.page.timer.stop();self.app.processEvents();self.refresh([self.h]);self.dialogs=[]
    def refresh(self,heads):
        self.page.ready(results.read(self.path,self.f.f.root/'ui-cache.sqlite3',self.h['day'],scope=[results.key(h) for h in heads]))
    def select(self,heads):
        self.page.chosen={results.key(h) for h in heads};self.page.render();self.page.controls()
    def tearDown(self):
        import shiboken6
        for d in self.dialogs:d.reject();shiboken6.delete(d)
        self.page.timer.stop();self.page.reader.shutdown();self.page.close();shiboken6.delete(self.page);self.app.processEvents()
        self.reader.stop();self.env.stop();self.f.tearDown()
    def test_date_view_can_correct_checked_order_without_resetting_scope(self):
        self.assertIsNone(self.page.batch_id);self.select([self.h]);before=copy.deepcopy(self.page.scope)
        self.assertTrue(self.page.reallocate_btn.isEnabled());self.assertIn('不能直接导出',self.page.status.text())
        with patch('jst_ui.generate') as generate:self.page.reallocate_btn.click()
        args=generate.call_args.args;self.assertEqual([results.key(h) for h in args[2]],[results.key(self.h)])
        self.assertEqual(args[3:],(self.bid,True));self.assertIsNone(self.page.batch_id);self.assertEqual(self.page.scope,before)
        self.assertEqual(self.page.chosen,{results.key(self.h)});self.assertFalse(self.page.reviewing)
    def test_prepare_on_checked_stale_orders_routes_to_original_batch(self):
        self.select([self.h])
        with patch('jst_ui.generate') as generate:self.page.prepare_btn.click()
        self.assertEqual(generate.call_args.args[3:],(self.bid,True))
    def test_previous_zero_allocation_returns_to_original_batch_after_amendment(self):
        self.f.f.save('return',[{'门店确认数量':0}]);p=batches.preview_existing(self.path,self.bid,[self.h],True);batches.execute(self.path,p)
        self.f.f.save('return',[{'门店确认数量':25}]);self.refresh([self.h]);self.select([self.h])
        self.assertTrue(self.page.reallocate_btn.isEnabled())
        with patch('jst_ui.generate') as generate:self.page.prepare_btn.click()
        self.assertEqual(generate.call_args.args[3:],(self.bid,True))
    def second_batch(self):
        other=self.f.second_store();r=batches.execute(self.path,batches.preview_new(self.path,[other],{'河北仓':fixture.catalog(stock=80)}))
        self.refresh([self.h,other]);self.select([self.h,other]);return other,r['batch_id']
    def test_multiple_batches_choose_one_and_keep_all_checked(self):
        other,bid=self.second_batch();chosen=set(self.page.chosen)
        def choose(parent,title,note,titles,*args):return next(t for t in titles if bid[:8] in t),True
        with patch('jst_results_ui.QInputDialog.getItem',side_effect=choose),patch('jst_ui.generate') as generate:self.page.reallocate_btn.click()
        args=generate.call_args.args;self.assertEqual([results.key(h) for h in args[2]],[results.key(other)])
        self.assertEqual(args[3:],(bid,True));self.assertEqual(self.page.chosen,chosen);self.assertIsNone(self.page.batch_id)
    def test_cancel_batch_choice_does_not_generate_or_change_selection(self):
        self.second_batch();chosen=set(self.page.chosen)
        with patch('jst_results_ui.QInputDialog.getItem',return_value=('',False)),patch('jst_ui.generate') as generate:self.page.reallocate_btn.click()
        generate.assert_not_called();self.assertEqual(self.page.chosen,chosen);self.assertFalse(self.page.reviewing)
    def test_pending_selected_order_is_not_silently_dropped(self):
        other=self.f.second_store();self.refresh([self.h,other]);self.select([self.h,other])
        with patch('jst_results_ui.QMessageBox.information') as notice,patch('jst_ui.generate') as generate:self.page.reallocate_btn.click()
        generate.assert_not_called();self.assertIn(other['number'],notice.call_args.args[2]);self.assertEqual(len(self.page.chosen),2)
    def test_preview_and_confirmation_explain_old_payment_is_preserved(self):
        from jst_ui import GenerationDialog
        def run(parent,fn,args,*a,**kw):return fn(*args)
        with patch('jst_ui.run',side_effect=run):
            d=GenerationDialog(self.page,self.host,[self.h],self.bid,True);self.dialogs.append(d);self.app.processEvents();d.preview_btn.click()
            self.assertIsNotNone(d.plan);self.assertTrue(d.generate_btn.isEnabled())
            self.assertIn('旧付款事实保留',d.summary.text());self.assertIn('需重新核对付款',d.summary.text())
            with patch('jst_ui.confirm',return_value=False) as confirm:d.generate_btn.click()
            self.assertIn('旧付款保留',confirm.call_args.args[2]);self.assertIsNone(d.worker)


if __name__=='__main__':unittest.main()
