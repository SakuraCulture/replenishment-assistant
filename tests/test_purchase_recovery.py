"""Offline accepted-task recovery, business messages and date explanations."""
import copy
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(Path(__file__).parent)]
from aoxiang_client import ComponentError
from aoxiang_workflow_runner import Runner
from aoxiang_workflows import Workflows
from aoxiang_workflow_state import counts,summary
from aoxiang_purchase_messages import explain,item_message
from runtime_support import Cancelled
from test_aoxiang_workflows import head


class Client:
    def __init__(self, pending=0):
        self.records={};self.calls=[];self.pending=pending;self.checks={};self.uploads=0
    def add(self,id,*,attempted=True,bound=False,task=True):
        self.records[id]={'id':id,'state':'BOUND' if bound else 'ACCEPTED' if attempted else 'READY',
            'active':False,'uploadAttempted':attempted,'taskId':'TASK-'+id if task and attempted else None,
            'binding':{'orderId':'PO-'+id,'status':'待提交'} if bound else None,'additionalTaskIds':[],
            'error':None}
    def request(self,action,**kw):
        self.calls.append((action,kw));id=kw.get('purchase_id')
        if action=='configuration':return {}
        if action=='purchase.status':return copy.deepcopy(self.records[id])
        if action=='purchase.pause':return copy.deepcopy(self.records[id])
        if action!='purchase.start':raise AssertionError(action)
        r=self.records[id]
        if not r['uploadAttempted']:
            self.uploads+=1;r.update(uploadAttempted=True,taskId='TASK-'+id)
        self.checks[id]=self.checks.get(id,0)+1
        if self.checks[id]<=self.pending:r.update(state='ACCEPTED',error={'code':'TASK_PENDING','message':'原任务尚未完成'})
        else:r.update(state='BOUND',binding={'orderId':'PO-'+id,'status':'待提交'},error=None)
        return copy.deepcopy(r)


class PurchaseRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.cancel=threading.Event()
        self.store=Workflows(self.root)
    def tearDown(self):self.tmp.cleanup()
    def runner(self,client,**kw):
        return Runner(self.store,client,ROOT/'components/aoxiang',self.root/'unused.sqlite3',cancel=self.cancel,
                      delay=.001,verification_interval=.001,verification_wait=kw.pop('verification_wait',.1),**kw)
    def item(self,id):return {'package':{'id':id}}
    def upload_batch(self,n=2):
        p=self.store.prepare([head(i) for i in range(n)])
        for i in range(n):self.store.update(p['id'],position=i,changes={'state':'PREPARED','package':{'id':str(i)}})
        p=self.store.update(p['id'],state='READY');return self.store.upload(p['id'],list(range(n)),p['sequence'])

    def test_actual_component_status_contract_resumes_known_task(self):
        script="""import {MachinePurchaseCreate} from './src/machine-purchase-create.mjs';
const r={id:'known',state:'ACCEPTED',sequence:7,uploadAttempted:true,receipt:{taskId:'TASK-known'},
intent:{basis:{},target:{},supplierCode:'S',rows:[],totalQuantity:'0',totalAmount:'0'},error:{code:'TASK_PENDING'}};
console.log(JSON.stringify(new MachinePurchaseCreate({ledger:{get:()=>r}}).status('known')));"""
        p=subprocess.run([str(ROOT/'components/aoxiang/runtime/node.exe'),'--input-type=module','-e',script],
                         cwd=ROOT/'components/aoxiang',text=True,encoding='utf-8',capture_output=True,check=True)
        public=json.loads(p.stdout);self.assertNotIn('receipt',public);self.assertEqual(public['taskId'],'TASK-known')
        c=Client();c.records['known']=public
        with patch('aoxiang_workflow_runner.validate_for_upload',side_effect=AssertionError('no new upload')):
            state,result=self.runner(c).upload_item(self.item('known'),{}, {})
        self.assertEqual(state,'BOUND');self.assertEqual(result['binding']['orderId'],'PO-known');self.assertEqual(c.uploads,0)
        self.assertEqual([a for a,_ in c.calls],['purchase.status','purchase.start','purchase.status'])

    def test_pending_is_observed_again_without_reupload(self):
        c=Client(pending=2);c.add('x');seen=[]
        result=self.runner(c).upload_item(self.item('x'),{},{},notify=lambda r:seen.append(r['state']))
        self.assertEqual(result[0],'BOUND');self.assertEqual(c.checks['x'],3);self.assertEqual(c.uploads,0)
        self.assertEqual(seen,['ACCEPTED','ACCEPTED','ACCEPTED','BOUND'])

    def test_first_upload_pending_then_completion_uploads_exactly_once(self):
        c=Client(pending=1);c.add('x',attempted=False)
        with patch('aoxiang_workflow_runner.validate_for_upload') as validate:
            self.assertEqual(self.runner(c).upload_item(self.item('x'),{}, {})[0],'BOUND')
        validate.assert_called_once();self.assertEqual(c.uploads,1);self.assertEqual(c.checks['x'],2)

    def test_unknown_and_conflicting_receipts_stay_blocked(self):
        for task,conflicts,code in [(False,[],'SUBMISSION_UNKNOWN'),(True,['OTHER'],'MULTIPLE_IMPORT_TASKS')]:
            c=Client();c.add('x',task=task);c.records['x']['additionalTaskIds']=conflicts
            with self.assertRaises(ComponentError) as error:self.runner(c).upload_item(self.item('x'),{},{})
            self.assertEqual(error.exception.code,code);self.assertEqual(c.uploads,0)
            self.assertEqual([a for a,_ in c.calls],['purchase.status'])

    def test_bound_is_reconciled_without_any_start(self):
        c=Client();c.add('x',bound=True)
        self.assertEqual(self.runner(c).upload_item(self.item('x'),{}, {})[0],'BOUND')
        self.assertEqual([a for a,_ in c.calls],['purchase.status'])

    def test_verified_void_is_terminal_association_not_purchase_success(self):
        job=self.upload_batch(1);c=Client();c.add('0',bound=True)
        c.records['0']['binding']['status']='已作废'
        after=self.runner(c).run(job['id']);item=after['items'][0]
        self.assertEqual(item['state'],'BOUND');self.assertEqual(counts(after)['voided'],1)
        self.assertEqual(counts(after)['success'],0);self.assertIn('已核实作废1单',summary(after))
        self.assertIn('返回删除页',item_message(item,'upload'))
        self.assertEqual([a for a,_ in c.calls if a.startswith('purchase.')],['purchase.status'])

    def test_bound_message_does_not_claim_live_platform_state(self):
        self.assertIn('上次核验',item_message({'state':'BOUND','result':{'binding':{'status':'待提交'}}},'upload'))
        self.assertNotIn('后续提交',item_message({'state':'BOUND'},'upload'))

    def test_platform_status_details_survive_runner_failure(self):
        job=self.upload_batch(1);c=Client();c.add('0')
        def failed(action,**kw):
            if action=='configuration':return {}
            if action=='purchase.start':return c.records['0']
            r=copy.deepcopy(c.records['0']);r.update(state='NEEDS_REVIEW',error={
                'code':'PURCHASE_STATUS_UNSUPPORTED','message':'状态不符','details':{'status':'已完成'}})
            return r
        c.request=failed;after=self.runner(c).run(job['id'])
        self.assertEqual(after['items'][0]['error']['details'],{'status':'已完成'})
        self.assertEqual(after['items'][0]['state'],'REVIEW')

    def test_cancel_while_waiting_preserves_original_task(self):
        c=Client(pending=100);c.add('x')
        def progress(_):self.cancel.set()
        with self.assertRaises(Cancelled):self.runner(c,progress=progress).upload_item(self.item('x'),{},{})
        self.assertEqual(c.records['x']['taskId'],'TASK-x');self.assertEqual(c.uploads,0);self.assertLessEqual(c.checks.get('x',0),1)

    def test_old_false_review_retries_same_batch_and_keeps_success(self):
        job=self.upload_batch();original=copy.deepcopy(job['items'])
        self.store.update(job['id'],position=0,changes={'state':'REVIEW','error':{'code':'SUBMISSION_UNKNOWN','message':'旧版本误报'}})
        self.store.update(job['id'],position=1,changes={'state':'BOUND','result':{'binding':{'orderId':'PO-1'}}})
        job=self.store.update(job['id'],state='COMPLETED_WITH_ERRORS');success=copy.deepcopy(job['items'][1])
        self.store.retry_items(job['id'],[0],job['sequence'])
        c=Client();c.add('0');c.add('1',bound=True)
        after=self.runner(c).run(job['id'])
        self.assertEqual(after['state'],'COMPLETED');self.assertEqual(after['items'][1],success)
        self.assertEqual(after['items'][0]['package'],original[0]['package']);self.assertEqual(c.uploads,0)
        self.assertEqual(after['items'][0]['purchase_observation']['orderId'],'PO-0')

    def test_pending_exhausts_bounded_wait_and_shows_waiting_not_failure(self):
        job=self.upload_batch(1);c=Client(pending=100);c.add('0')
        after=self.runner(c,verification_wait=0).run(job['id'])
        self.assertEqual(c.checks['0'],1);self.assertEqual(c.uploads,0)
        self.assertEqual(counts(after)['waiting'],1);self.assertEqual(counts(after)['review'],0)
        self.assertIn('已受理待核对1单',summary(after));self.assertNotIn('失败',summary(after))
        self.assertIn('稍后',item_message(after['items'][0],'upload'))
        self.store.retry_items(job['id'],[0],after['sequence']);c.pending=0
        self.assertEqual(self.runner(c).run(job['id'])['state'],'COMPLETED');self.assertEqual(c.uploads,0)

    def test_explanations_have_action_and_keep_unknown_uncertain(self):
        text=explain({'code':'SUBMISSION_UNKNOWN'}, {'uploadAttempted':True})
        self.assertIn('可能已建单',text);self.assertIn('单据详情',text)
        self.assertIn('已找到原受理任务号',explain({'code':'SUBMISSION_UNKNOWN'},{'taskId':'T'}))
        self.assertIn('失败商品',explain({'code':'IMPORT_TASK_PARTIAL'}))


class DateMessageTests(unittest.TestCase):
    def test_baseline_date_mismatch_names_actual_date(self):
        from stock_diagnosis import strict_baseline
        old={'S':{'store_name':'合成店','rows':[{'E':'001','D':'合成商品','AS':4,'_meta':{'admission':{'checked_day':'2026-11-06'}}}]}}
        new=copy.deepcopy(old);new['S']['rows'][0]['_meta']['admission']['checked_day']='2026-11-07'
        with self.assertRaises(ValueError) as error:strict_baseline(old,new)
        for text in ('准入核验日期','2026-11-06','2026-11-07','001','重新计算'):self.assertIn(text,str(error.exception))
        self.assertNotIn('：rows',str(error.exception));self.assertEqual(old['S']['rows'][0]['AS'],4)

    def test_baseline_quantity_difference_is_not_described_as_date_change(self):
        from stock_diagnosis import strict_baseline
        old={'S':{'rows':[{'E':'001','AS':4}]}};new=copy.deepcopy(old);new['S']['rows'][0]['AS']=5
        with self.assertRaises(ValueError) as error:strict_baseline(old,new)
        self.assertIn('模型补货量',str(error.exception));self.assertIn('当前结果 4，重算结果 5',str(error.exception))
        self.assertNotIn('核验日期由',str(error.exception))

    def test_crossday_trial_stops_before_preparation(self):
        import stock_diagnosis
        with patch('work_repository.today',return_value='2026-11-07'),patch.object(stock_diagnosis.automation_jobs,'prepare') as prepare:
            with self.assertRaisesRegex(ValueError,'作业已跨天'):stock_diagnosis.run({'day':'2026-11-06'},{},{})
        prepare.assert_not_called()

    def test_unsigned_previous_day_is_not_sent_to_nonexistent_signed_history(self):
        from job_date_messages import export_previous
        unsigned=export_previous('2026-11-06','2026-11-07',False)
        self.assertIn('尚未签发',unsigned);self.assertIn('用此配置新建今日草稿',unsigned)
        self.assertNotIn('导出原已签发版本',unsigned)
        self.assertIn('导出原已签发版本',export_previous('2026-11-06','2026-11-07',True))


if __name__=='__main__':unittest.main()
