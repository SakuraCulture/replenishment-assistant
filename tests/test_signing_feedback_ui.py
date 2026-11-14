"""Signing entry feedback must describe the current attempt and known effects."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import unittest
from datetime import date,timedelta
from types import SimpleNamespace as NS
from unittest.mock import Mock,patch
import work_repository as wr
import readable_ui
from application_window import ApplicationWindow


class SigningFeedbackTests(unittest.TestCase):
    def host(self):
        status=Mock();status.setText('上一次签发导出失败')
        return NS(worker=None,readonly=False,input_dirty=False,job_day=wr.today(),status=status,
                  states={'A':{'name':'甲店','state':'已完成'}},selected_codes=lambda:['A'],
                  work_repository=NS(path='unused-private.sqlite3'),snapshot=Mock(return_value={}),
                  failed=Mock(),refresh_job_label=Mock(),refresh_archive=Mock(),delivery_done=Mock())

    def test_busy_or_dirty_entries_explain_without_starting_a_second_operation(self):
        for method in ('confirm_normal','export_current'):
            for field,value,word in [('worker',object(),'另一个作业'),('input_dirty',True,'输入资料已变化')]:
                with self.subTest(method=method,field=field):
                    host=self.host();setattr(host,field,value)
                    with patch('interaction_ui.choose_stores') as choose,patch('merchant_selection.MerchantSelection') as selection,patch('ui_work_io.Session') as session:
                        getattr(ApplicationWindow,method)(host)
                    choose.assert_not_called();selection.assert_not_called();session.assert_not_called()
                    self.assertIn(word,host.status.setText.call_args.args[0]);host.failed.assert_not_called()

    def test_new_signature_readonly_and_wrong_day_explain_before_selection(self):
        for field,value,word in [('readonly',True,'只读工作区'),('job_day',(date.fromisoformat(wr.today())-timedelta(days=1)).isoformat(),'不是今日')]:
            with self.subTest(field=field):
                host=self.host();setattr(host,field,value)
                with patch('interaction_ui.choose_stores') as choose,patch('ui_work_io.Session') as session:ApplicationWindow.confirm_normal(host)
                choose.assert_not_called();session.assert_not_called();host.failed.assert_not_called()
                self.assertIn(word,host.status.setText.call_args.args[0])

    def test_cancelled_selection_replaces_old_error_and_does_not_sign(self):
        host=self.host()
        with patch('interaction_ui.choose_stores',return_value=[]),patch('ui_work_io.Session') as session:ApplicationWindow.confirm_normal(host)
        session.assert_not_called();host.failed.assert_not_called()
        self.assertIn('本次未发起签发',host.status.setText.call_args.args[0])
        self.assertNotIn('上一次',host.status.setText.call_args.args[0])

    def confirmation(self,host,*,accepted,refresh_error=False):
        cancel,accept,review=object(),object(),object();box=Mock()
        box.addButton.side_effect=[cancel,accept,review];box.clickedButton.return_value=accept if accepted else cancel
        receipt={'saved':'2026-11-13T10:00:00+08:00','orders':[{'number':'BH-20261113-A-ZC','revision':3}]}
        def request(parent,session,operation,value):
            return {'lines':[],'rows':[],'total':0} if operation=='prepare_normal' else receipt
        if refresh_error:host.refresh_archive.side_effect=RuntimeError('isolated index failure')
        with patch('interaction_ui.choose_stores',return_value=['A']),patch('signing_guidance.require_applied',return_value=True),patch('ui_work_io.Session'),patch('ui_work_io.request',side_effect=request) as called,patch('readable_ui.ReviewDialog',return_value=box),patch('readable_ui.review_table'):
            ApplicationWindow.confirm_normal(host)
        return called

    def test_confirmation_cancel_does_not_submit_and_reports_this_attempt(self):
        host=self.host();called=self.confirmation(host,accepted=False)
        self.assertEqual([c.args[2] for c in called.call_args_list],['prepare_normal'])
        self.assertIn('已取消本次签发确认',host.status.setText.call_args.args[0]);host.failed.assert_not_called()

    def test_success_lists_exact_identity_even_when_refresh_fails(self):
        host=self.host();called=self.confirmation(host,accepted=True,refresh_error=True)
        self.assertEqual([c.args[2] for c in called.call_args_list],['prepare_normal','confirm_normal'])
        self.assertTrue(host.tracking_stale);self.assertFalse(host.job_dirty)
        self.assertEqual(host.saved_time,'2026-11-13T10:00:00+08:00')
        text=host.status.setText.call_args.args[0]
        for word in ('已自动归档','BH-20261113-A-ZC V003','列表刷新未完成'):self.assertIn(word,text)
        host.failed.assert_not_called()

    def test_historical_same_revision_export_still_works_without_new_signature(self):
        host=self.host();host.readonly=True;host.job_day='2020-01-01'
        plans=[{'same':True,'name':'甲店','old':{'revision':2},'files':[{'filename':'signed.xlsx'}]}]
        receipt={'paths':['private/signed.xlsx'],'signed':False,'saved':None}
        def request(parent,session,operation,value):
            return {'plans':plans} if operation=='delivery_probe' else receipt
        with patch('signing_guidance.require_applied',return_value=True),patch('normal_delivery.probe_payload',return_value={}),patch('ui_work_io.Session'),patch('ui_work_io.request',side_effect=request) as calls,patch('interaction_ui.confirm',return_value=True),patch('export_locations.save_file',return_value=('private/signed.xlsx','')):
            ApplicationWindow.export_current(host,['A'])
        self.assertEqual([c.args[2] for c in calls.call_args_list],['delivery_probe','delivery_execute'])
        host.delivery_done.assert_called_once_with(receipt)

    def test_readonly_changed_revision_does_not_reach_signing_expand_or_execute(self):
        host=self.host();host.readonly=True
        with patch('signing_guidance.require_applied',return_value=True),patch('normal_delivery.probe_payload',return_value={}),patch('ui_work_io.Session'),patch('ui_work_io.request',return_value={'plans':[{'same':False}]}) as calls:
            ApplicationWindow.export_current(host,['A'])
        self.assertEqual([c.args[2] for c in calls.call_args_list],['delivery_probe'])
        self.assertIn('只读工作区',host.status.setText.call_args.args[0]);host.delivery_done.assert_not_called()

    def test_export_success_is_not_changed_into_failure_by_result_dialog(self):
        host=self.host();receipt={'paths':['private/signed.xlsx'],'signed':True,'saved':'saved',
             'orders':[{'number':'BH-20261113-A-ZC','revision':2},{'number':'BH-20261113-A-ZC','revision':2}]}
        with patch('business_scenes.issue_result',side_effect=RuntimeError('isolated display failure')):ApplicationWindow.delivery_done(host,receipt)
        text=host.status.setText.call_args.args[0]
        self.assertIn('已导出 1 份',text);self.assertEqual(text.count('BH-20261113-A-ZC V002'),1)
        self.assertIn('结果展示未完成',text);self.assertTrue(host.tracking_stale);host.failed.assert_not_called()


class FailureMessageTests(unittest.TestCase):
    def test_structured_failure_precedes_long_paths_and_preserves_commit_uncertainty(self):
        raw='导出操作未全部完成：失败阶段：校验签发快照\n原因：MemoryError：异常未提供文字说明\n提交状态：已提交\n签发结果待核。\n已生成 0/20 份：无\n未生成：D:\\very-long.xlsx\n诊断记录：private/work-io-123.json\n可到“订单跟踪 → 导出任务与恢复”核对。'
        text=readable_ui.failure_message(raw)
        for word in ('失败阶段：校验签发快照','原因：MemoryError','提交状态：已提交','已生成 0/20 份','private/work-io-123.json'):self.assertIn(word,text)
        self.assertLess(text.index('失败阶段'),text.index('已生成'))
        for word in ('关闭占用文件','更换可写目录','D:\\very-long.xlsx'):self.assertNotIn(word,text)

    def test_legacy_unknown_signature_does_not_claim_rollback_or_file_lock(self):
        raw='导出操作未全部完成：签发结果待核。\n已生成 0/20 份：无\n未生成：D:\\twenty-paths.xlsx\n\n可到“订单跟踪 → 导出任务与恢复”查看逐文件结果。'
        text=readable_ui.failure_message(raw)
        for word in ('提交状态尚未核实','不能据此判断是否已经签发','0/20','不要重复签发'):self.assertIn(word,text)
        for word in ('关闭占用文件','更换可写目录','未提交','已回滚'):self.assertNotIn(word,text)

    def test_plain_confirmation_structured_error_is_not_hidden(self):
        raw='归档操作未完成，请在历史记录核对状态：失败阶段：等待写锁\n原因：OperationalError：database is locked\n提交状态：未开始提交\n诊断记录：private/work-io-456.json'
        text=readable_ui.failure_message(raw)
        for word in ('等待写锁','OperationalError','未开始提交','private/work-io-456.json'):self.assertIn(word,text)
        self.assertNotIn('当前操作未完成。请查看',text)


if __name__=='__main__':unittest.main()
