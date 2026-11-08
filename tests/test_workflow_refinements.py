"""UI scope and selection protections; only isolated application settings."""
import os,sys,unittest,copy
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/p) for p in ('src','tests','tools')]
import test_jst_ui as qt_fixture


class WorkflowRefinements(unittest.TestCase):
    setUp=qt_fixture.JstUI.setUp
    tearDown=qt_fixture.JstUI.tearDown
    @classmethod
    def setUpClass(cls):
        qt_fixture.JstUI.setUpClass();cls.app=qt_fixture.JstUI.app
    def test_move_adopts_manual_order_and_locked_batch_does_not_change_strategy(self):
        from jst_ui import GenerationDialog
        from PySide6.QtCore import QModelIndex
        heads=[dict(number='N'+str(i),name='合成店',store='S',warehouse='河北仓',oid=str(i)) for i in range(2)]
        with patch.object(GenerationDialog,'load_catalogs'):
            d=GenerationDialog(self.win,self.win,heads);self.app.processEvents()
            self.assertEqual(d.order_mode.currentIndex(),0)
            self.assertTrue(d.order_list.model().moveRows(QModelIndex(),0,1,QModelIndex(),2))
            self.assertEqual(d.order_mode.currentIndex(),1);self.assertEqual(d.order_list.item(0).data(256)['number'],'N1')
            self.assertIsNone(d.plan);self.assertFalse(d.generate_btn.isEnabled())
            d.batch_id='fixed';d.apply_mode();self.assertFalse(d.order_mode.isEnabled());self.assertFalse(d.order_list.isEnabled());self.assertFalse(d.restore_order_btn.isEnabled())
            d.order_mode.setCurrentIndex(0);d.order_moved();self.assertEqual(d.order_mode.currentIndex(),0)
            d.reject();d.deleteLater()
    def test_completed_buttons_show_scope_no_selection_warns_and_lock_only_selected(self):
        from jst_ui import GenerationDialog
        with patch.object(GenerationDialog,'load_catalogs'):
            d=GenerationDialog(self.win,self.win,[]);self.app.processEvents()
            for i in range(2):d.saved_order(dict(positive=True,gid='G'+str(i),number='N'+str(i)))
            self.assertFalse(d.lock_saved_btn.isEnabled());self.assertIn('已选择0单',d.completed_note.text())
            with patch('jst_ui.QMessageBox.information') as notice:d.lock_saved();notice.assert_called_once()
            d.completed.item(1).setSelected(True);self.assertEqual(d.gids(),['G1']);self.assertTrue(d.lock_saved_btn.isEnabled())
            with patch('jst_ui.confirm',return_value=True),patch('jst_ui.run') as run:
                d.lock_saved_btn.click();self.assertEqual(run.call_args.args[2],(d.path,['G1']))
            d.select_completed_btn.click();self.assertEqual(set(d.gids()),{'G0','G1'});self.assertIn('2单',d.lock_saved_btn.text())
            d.reject();d.deleteLater()
    def test_purchase_error_messages_separate_known_acceptance_from_query_failures(self):
        import aoxiang_purchase_messages as messages
        known={'taskId':'EXACT-OLD-TASK','uploadAttempted':True}
        for code in ('IDENTITY_RESPONSE_TIMEOUT','IDENTITY_CHECK_FAILED','PURCHASE_STORE_NOT_FOUND','IMPORT_SIDE_EFFECT_BLOCKED'):
            text=messages.explain({'code':code},known);self.assertNotEqual(text,'当前没有异常记录。');self.assertTrue('原任务' in text or '受理回执' in text)
        self.assertIn('尚不能判断登录失效',messages.explain({'code':'IDENTITY_RESPONSE_TIMEOUT'},known))
        self.assertIn('门店',messages.explain({'code':'BROWSER_OPERATION_FAILED','message':'locator .next-tree-node-label timeout'},known))
        r={'uploadWarning':{'code':'IMPORT_SIDE_EFFECT_BLOCKED'},'state':'BOUND','binding':{'orderId':'PO1'}}
        self.assertEqual(messages.observation(r)['uploadWarning'],r['uploadWarning'])
    def test_review_scope_filters_do_not_modify_whole_batch_or_confirmation_rules(self):
        import test_jst_payment as fixture,jst_payment
        from lifecycle_ui import Review
        from order_lifecycle import fingerprint
        f=fixture.DirectPayment();f.setUp()
        try:
            plan=jst_payment.prepare(f.path,f.entries);before=fingerprint(plan);d=Review(self.win,plan);d.show();self.app.processEvents()
            self.assertEqual(d.model.rowCount(),4);root=d.scope_tree.topLevelItem(0)
            d.scope_tree.setCurrentItem(root.child(0).child(0));self.assertEqual(d.model.rowCount(),1)
            self.assertIn('本批全部 4 份',d.view_scope.text());self.assertEqual(fingerprint(plan),before)
            self.assertFalse(d.save.isEnabled());d.complete.setChecked(True);self.assertTrue(d.save.isEnabled())
            d.review_tabs.setCurrentIndex(1);d.review_tabs.setCurrentIndex(2);d.scope_tree.setCurrentItem(root);self.assertEqual(d.model.rowCount(),4)
            self.assertEqual(fingerprint(plan),before);self.assertTrue(f.unpaid());d.reject();d.deleteLater()
        finally:f.tearDown()


if __name__=='__main__':unittest.main()
