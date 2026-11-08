import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock,patch
import work_repository as wr
import signing_guidance as g


class SigningGuidanceTests(unittest.TestCase):
    def host(self):
        return NS(worker=None,readonly=False,input_dirty=False,job_day=wr.today(),states={'A':{'name':'甲店'},'B':{'name':'乙店'}},preissue_drafts={'A':{},'B':{}},selected_codes=lambda:['A'],open_final_list=Mock())
    def test_only_selected_pending_stores_and_no_implicit_application(self):
        h=self.host();self.assertEqual(g.pending_stores(h,['A']),['A'])
        with patch.object(g,'QMessageBox') as factory:
            box=factory.return_value;cancel,review=object(),object();box.addButton.side_effect=[cancel,review];box.clickedButton.return_value=review
            self.assertFalse(g.require_applied(h,['A']))
        h.open_final_list.assert_called_once_with(codes=['A'])
        self.assertIn('甲店（A）',box.setText.call_args.args[0]);self.assertNotIn('乙店',box.setText.call_args.args[0])
        box.setDefaultButton.assert_called_once_with(cancel);self.assertEqual(h.preissue_drafts,{'A':{},'B':{}})
    def test_cancel_keeps_pending_and_unselected_pending_does_not_block(self):
        h=self.host()
        with patch.object(g,'QMessageBox') as factory:
            box=factory.return_value;box.clickedButton.return_value=None
            self.assertFalse(g.require_applied(h,['A']))
        h.open_final_list.assert_not_called()
        with patch.object(g,'QMessageBox') as factory:
            self.assertTrue(g.require_applied(h,['unaffected']));factory.assert_not_called()
    def test_both_actual_main_entries_stop_before_creating_a_worker(self):
        from application_window import ApplicationWindow
        h=self.host()
        with patch('interaction_ui.choose_stores',return_value=['A']),patch('signing_guidance.require_applied',return_value=False) as guard,patch('ui_work_io.Session') as session:
            ApplicationWindow.confirm_normal(h);guard.assert_called_once_with(h,['A']);session.assert_not_called()
        with patch('signing_guidance.require_applied',return_value=False) as guard,patch('ui_work_io.Session') as session:
            ApplicationWindow.export_current(h,['A']);guard.assert_called_once_with(h,['A']);session.assert_not_called()
