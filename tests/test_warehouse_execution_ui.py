"""Execution scope is a post-calculation choice; isolated UI and actual exports."""
import copy,hashlib,os,sys,tempfile,unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import normal_delivery as delivery,normal_orders,work_repository as wr
import warehouse_execution as execution
import test_signing_failures as fixtures


class WarehouseExecutionDelivery(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.SigningFailures();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.repo=self.fixture.repo;self.path=self.fixture.work.path;self.payload=self.fixture.payload()
        self.output=self.fixture.output

    def prepared(self,warehouses):
        return delivery.prepare_owned_batch(self.repo,copy.deepcopy(self.payload),['S1'],warehouses={'S1':warehouses})

    def child_heads(self):
        with closing(self.repo.work.connect()) as db:return execution.heads(db)

    def test_only_execution_scope_changes_full_two_warehouse_calculation_stays_exact(self):
        before=copy.deepcopy(self.payload);plan=self.prepared(['山东仓'])
        self.assertEqual(self.payload,before)
        self.assertEqual(plan['payload']['results'],before['results'])
        self.assertEqual(plan['warehouses'],{'S1':['山东仓']})
        self.assertEqual([f['warehouse'] for f in plan['plans'][0]['files']],['山东仓'])
        self.assertEqual(len(plan['payload']['results']['S1']['rows']),2)

    def test_own_scope_is_carried_by_small_preflight_without_changing_result_or_route(self):
        self.payload['execution_warehouses']={'S1':['山东仓']}
        before=copy.deepcopy(self.payload);slim=delivery.probe_payload(self.payload,['S1'])
        self.assertEqual(slim['execution_warehouses'],before['execution_warehouses'])
        self.assertEqual(slim['results'],before['results'])
        self.assertEqual(self.payload,before)
        self.assertEqual(delivery.prepare_owned_batch(self.repo,slim,['S1'])['warehouses'],{'S1':['山东仓']})

    def test_invalid_or_empty_explicit_scope_cannot_fall_back_to_both(self):
        for warehouses in ([],['不存在仓'],['河北仓','河北仓']):
            with self.subTest(warehouses=warehouses),self.assertRaises(ValueError):self.prepared(warehouses)
        self.assertEqual(self.repo.headers(),[])

    def test_first_hb_then_add_sd_same_calculation_only_activates_chosen_children(self):
        first=delivery.execute_batch(self.repo,self.prepared(['河北仓']),self.output)
        self.assertEqual(len(first['paths']),1)
        heads=self.child_heads();self.assertEqual([h['warehouse'] for h in heads],['河北仓'])
        hb_before=copy.deepcopy(heads[0]);plan=self.prepared(['山东仓'])
        self.assertFalse(plan['plans'][0]['same']);self.assertTrue(plan['plans'][0]['parent_same'])
        second=delivery.execute_batch(self.repo,plan,self.output)
        self.assertEqual(len(second['paths']),1)
        self.assertEqual({h['warehouse'] for h in self.child_heads()},{'河北仓','山东仓'})
        hb_after=next(h for h in self.child_heads() if h['warehouse']=='河北仓')
        self.assertEqual(hb_after,hb_before)
        self.assertEqual(len(self.repo.headers()),1)

    def test_export_receipt_and_hidden_identity_use_the_warehouse_pinned_source(self):
        import openpyxl,order_identity,export_receipts
        delivery.execute_batch(self.repo,self.prepared(['河北仓','山东仓']),self.output)
        first={h['warehouse']:h for h in self.child_heads()}
        # Change only HB's signed quantity and publish only HB. SD remains V1.
        row=next(r for r in self.payload['results']['S1']['rows'] if r['AU']=='河北仓')
        row['AS']=row['AS']+1
        import result_semantics
        self.payload['results']['S1']['summary']=result_semantics.summarize(self.payload['results']['S1']['rows'])
        delivery.execute_batch(self.repo,self.prepared(['河北仓']),self.output)
        heads={h['warehouse']:h for h in self.child_heads()}
        self.assertGreater(heads['河北仓']['revision'],first['河北仓']['revision'])
        self.assertEqual(heads['山东仓'],first['山东仓'])
        receipt=delivery.execute_batch(self.repo,self.prepared(['山东仓']),self.output)
        self.assertEqual(receipt['orders'][0]['warehouse_heads'],[first['山东仓']])
        task=next(t for t in export_receipts.recent(self.path) if t['id']==receipt['export_task'])
        self.assertEqual(task['files'][0]['head']['revision'],first['山东仓']['revision'])
        self.assertEqual(task['files'][0]['head']['digest'],first['山东仓']['digest'])
        wb=openpyxl.load_workbook(receipt['paths'][0],read_only=True)
        try:refs=order_identity.read(wb)
        finally:wb.close()
        self.assertEqual({r['revision'] for r in refs},{first['山东仓']['revision']})
        self.assertEqual({r['digest'] for r in refs},{first['山东仓']['digest']})

    def test_deleted_child_cannot_be_republished_by_implicit_both_selection(self):
        delivery.execute_batch(self.repo,self.prepared(['河北仓','山东仓']),self.output)
        oid=self.repo.headers()[0]['oid']
        with closing(self.repo.work.connect()) as db,db:execution.retire(db,oid,'河北仓','synthetic-test')
        with self.assertRaisesRegex(wr.Conflict,'永久删除'):self.prepared(['河北仓','山东仓'])
        self.assertTrue(self.prepared(['山东仓'])['plans'][0]['parent_same'])

    def test_actual_work_process_preserves_execution_scope_through_confirmation_and_draft(self):
        from ui_work_io import Session
        self.payload['execution_warehouses']={'S1':['山东仓']}
        before=copy.deepcopy(self.payload['results'])
        with Session(self.path) as session:
            prepared=session.call('prepare_normal',[self.payload,['S1']],lambda _:None)
            self.assertIn('执行仓：山东仓',prepared['lines'][0])
            receipt=session.call('confirm_normal',None,lambda _:None)
        children=receipt['orders'][0]['warehouse_heads']
        self.assertEqual([h['warehouse'] for h in children],['山东仓'])
        self.assertEqual([h['warehouse'] for h in self.child_heads()],['山东仓'])
        saved=self.fixture.work.get_job(wr.today())['payload']
        self.assertEqual(saved['execution_warehouses'],{'S1':['山东仓']})
        self.assertEqual(saved['results'],before)

    def test_actual_work_process_delivery_carries_the_same_selected_warehouse(self):
        from ui_work_io import Session
        before=copy.deepcopy(self.payload['results'])
        with Session(self.path) as session:
            slim=delivery.probe_payload(self.payload,['S1'])
            probe=session.call('delivery_probe',[slim,['S1'],{'S1':['山东仓']}],lambda _:None)
            self.assertEqual([f['warehouse'] for f in probe['plans'][0]['files']],['山东仓'])
            tail={**self.payload,'results':{}}
            session.call('delivery_expand',[tail,['S1']],lambda _:None)
            receipt=session.call('delivery_execute',str(self.output),lambda _:None)
        self.assertEqual(len(receipt['paths']),1)
        self.assertEqual([h['warehouse'] for h in self.child_heads()],['山东仓'])
        self.assertEqual(self.fixture.work.get_job(wr.today())['payload']['results'],before)

    def test_late_dual_warehouse_calculation_cannot_restore_deleted_sibling(self):
        from application_window import ApplicationWindow
        from deletion_barrier import mark_warehouses,read_warehouse_epochs
        delivery.execute_batch(self.repo,self.prepared(['河北仓','山东仓']),self.output)
        captured=read_warehouse_epochs(self.path,wr.today(),{'S1':['河北仓','山东仓']})
        hb=next(h for h in self.child_heads() if h['warehouse']=='河北仓')
        with closing(self.repo.work.connect()) as db,db:mark_warehouses(db,[hb])
        worker=SimpleNamespace(deletion_generations={},warehouse_deletion_generations=captured)
        marker=object()
        host=SimpleNamespace(states={'S1':{'state':'计算中'}},sender=lambda:worker,
                             work_repository=self.repo.work,job_day=wr.today(),new_bases={'S1':marker})
        # It must return before super() or any result/state publication: this
        # incomplete host deliberately cannot accept a legitimate callback.
        ApplicationWindow.task_changed(host,'S1','已完成',self.payload['results']['S1'],None)
        self.assertIs(host.new_bases['S1'],marker)
        self.assertEqual(host.states['S1']['state'],'计算中')


class WarehouseExecutionSelectors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        from PySide6.QtWidgets import QWidget
        self.fixture=fixtures.SigningFailures();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.payload=self.fixture.payload();self.host=QWidget();self.windows=[]
        self.host.results=copy.deepcopy(self.payload['results']);self.host.states=copy.deepcopy(self.payload['states'])
        self.host.new_focus=None;self.host.job_day=wr.today();self.host.execution_warehouses={}
        self.host.work_repository=self.fixture.work

    def tearDown(self):
        for window in reversed(self.windows):window.close();window.deleteLater()
        self.host.close();self.host.deleteLater();self.app.processEvents()

    def test_signed_selection_only_sd_preserves_two_warehouse_results(self):
        from warehouse_execution_ui import ExecutionSelection
        from PySide6.QtCore import Qt
        dialog=ExecutionSelection(self.host,self.host.states,['S1']);self.windows.append(dialog)
        original=copy.deepcopy(self.host.results)
        for store,warehouse,item,valid in dialog.items:
            item.setCheckState(Qt.CheckState.Checked if warehouse=='山东仓' else Qt.CheckState.Unchecked)
        self.assertEqual(dialog.warehouses(),{'S1':['山东仓']})
        self.assertEqual(self.host.results,original);self.assertTrue(dialog.go.isEnabled())

    def test_native_selection_cancel_and_accept_do_not_edit_results(self):
        from PySide6.QtCore import QTimer,Qt
        from PySide6.QtWidgets import QApplication
        from interaction_ui import choose_stores
        original=copy.deepcopy(self.host.results)
        QTimer.singleShot(0,lambda:QApplication.activeModalWidget().reject())
        self.assertEqual(choose_stores(self.host,'scope',self.host.states,['S1'],execution=True),[])
        self.assertEqual(self.host.execution_warehouses,{})
        def choose():
            dialog=QApplication.activeModalWidget()
            for store,warehouse,item,valid in dialog.items:item.setCheckState(Qt.CheckState.Checked if warehouse=='山东仓' else Qt.CheckState.Unchecked)
            dialog.go.click()
        QTimer.singleShot(0,choose)
        self.assertEqual(choose_stores(self.host,'scope',self.host.states,['S1'],execution=True),['S1'])
        self.assertEqual(self.host.execution_warehouses,{'S1':['山东仓']});self.assertEqual(self.host.results,original)

    def test_merchant_selection_uses_saved_round_scope_and_disables_retired_child(self):
        from merchant_selection import MerchantSelection
        self.host.execution_warehouses={'S1':['山东仓']}
        dialog=MerchantSelection(self.host);self.windows.append(dialog)
        self.assertEqual(dialog.warehouses(),{'S1':['山东仓']})
        self.assertIn('未选仓',dialog.findChildren(__import__('PySide6.QtWidgets',fromlist=['QLabel']).QLabel)[0].text())
        delivery.execute_batch(self.fixture.repo,delivery.prepare_batch(self.fixture.repo,self.payload,['S1']),self.fixture.output)
        oid=self.fixture.repo.headers()[0]['oid']
        with closing(self.fixture.work.connect()) as db,db:execution.retire(db,oid,'河北仓','synthetic-ui-test')
        self.host.execution_warehouses={}
        second=MerchantSelection(self.host);self.windows.append(second);second.select_all(True)
        self.assertEqual(second.warehouses(),{'S1':['山东仓']})
        self.assertFalse(next(valid for store,w,item,valid in second.items if w=='河北仓'))


if __name__=='__main__':unittest.main()
