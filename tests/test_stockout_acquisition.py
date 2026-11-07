import copy
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from tests.test_stockout_analysis import workbook
from stockout_repository import Repository
from stockout_common import file_hash,TZ
from stockout_acquisition import execute
from aoxiang_jobs import Jobs,fingerprint
from runtime_support import Cancelled
import stockout_trigger as trigger


class NativeFixture:
    """Deterministic native protocol fixture. Creates no platform/browser connection."""
    def __init__(self,root,cancel):
        self.root=root;self.cancel=cancel;self.calls=[];self.commands={};self.observation=None
        self.ack=None;self.operation=None;self.pause_on_reserve=False;self.unknown_once=False;self.mapping='m1'
        folder=root/'jst/downloads/batch/item';folder.mkdir(parents=True)
        source=workbook(folder/'source.xlsx',['商品编码','分类','基本售价','出库箱规','商品状态','可用数','供应商名称','虚拟分类'],
                        [['A','饮品',2,1,'启用',0,'供应商甲',''],['B','饮品',2,1,'禁用',0,'供应商乙','淘便考核']])
        from catalog_sync_validation import validate
        self.receipt=validate({'schema':1,'file':str(source),'warehouse':'山东仓','sha256':file_hash(source),'expected_rows':2,'candidate':str(folder/'candidate.json')})

    def rpc(self,service,**payload):
        self.calls.append((service,copy.deepcopy(payload)))
        if service=='ax_root':return str(self.root/'ax')
        if service=='jst_root':return str(self.root/'jst')
        if service in {'release_ax','release_jst'}:return True
        if service=='observation':return {'warehouse':payload['warehouse'],'state':None,'included':[],'observed_at':'2026-11-07T08:00:00+08:00'}
        action=payload['action'];v=payload['values']
        if service=='jst':
            if action=='sync.start':
                assert v['purpose']=='stockout_analysis'
                reused=self.operation is not None
                if reused:assert self.operation==v['operation_id']
                self.operation=v['operation_id'];return {'batch_id':'batch','reused':reused}
            if action=='sync.status':
                item={'id':'item','account':{'warehouse':'山东仓','expectedCoid':'C','expectedUid':'U'},
                      'stage':'READY' if self.observation else 'WAIT_HOST','observationRequest':'token','observation':self.observation,
                      'native':{'scope':{'coid':'C','uid':'U'},'sha256':self.receipt['source_sha256']},
                      'receipt':self.receipt if self.observation else None,'adoption':self.ack}
                return {'running':not bool(self.observation),'batches':[{'id':'batch','operation_id':self.operation,'purpose':'stockout_analysis','items':[item]}]}
            if action=='sync.provide_observation':
                assert self.observation is None;assert v['token']=='token';self.observation=v['observation'];return {'accepted':True}
            if action=='sync.acknowledge_adoption':self.ack=v['receipt'];return {'accepted':True}
            if action=='sync.stop':self.observation=self.observation or {'stopped':True};return {}
            if action=='sync.resume':return {}
            raise AssertionError(action)
        if action=='configuration':return {'configured':True,'enabled':True,'account_binding':{'account_id':'A','merchant_code':'M'},
            'store_mapping_version':self.mapping,'targets':[{'buyerWarehouseCode':'S','displayName':'测试门店'}]}
        if action=='reserve':
            command=v['command'];cid=command['command_id']
            if cid not in self.commands:
                folder=self.root/'ax/command-files'/cid;folder.mkdir(parents=True)
                source=workbook(folder/'source.xlsx',['仓库/门店编码','商品编码','商品名称','月销','可用库存','在途库存'],
                                [['S','A','商品A',1,0,6],['S','B','商品B',1,2,0]])
                sha=file_hash(source);destination=folder/('0-'+sha+'.xlsx');source.rename(destination)
                result={'warehouse_code':'S','run_id':'native-'+cid,'file':str(destination.resolve()),'sha256':sha,'bytes':destination.stat().st_size,
                        'rows':2,'account_binding':command['account_binding'],'collected_at':'2026-11-07T08:00:00+08:00'}
                self.commands[cid]={'id':cid,'fingerprint':fingerprint(command),'request':command,'sequence':1,'state':'SUCCEEDED',
                    'items':[{'index':0,'warehouse_code':'S','run_id':'native-'+cid,'state':'SUCCEEDED','result':result,'error':None}]}
                if self.pause_on_reserve:self.commands[cid]['state']='RUNNING';self.cancel.set()
                if self.unknown_once:self.unknown_once=False;raise RuntimeError('受理响应中断，结果未知')
            return {'record':copy.deepcopy(self.commands[cid])}
        if action=='status':return copy.deepcopy(self.commands[v['command_id']])
        if action in {'resume','pause'}:
            row=self.commands[v['command_id']];row['sequence']+=1;row['state']='PAUSED' if action=='pause' else 'SUCCEEDED';return copy.deepcopy(row)
        raise AssertionError(action)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.repo=Repository(self.root/'settings');self.repo.initialize()
        self.repo.save_settings({'stores':[{'code':'S','name':'测试门店'}],'routes':{'S':{'primary':'山东仓','secondary':None}},'profile':None})
        self.cancel=threading.Event();self.native=NativeFixture(self.root,self.cancel)

    def run_it(self,rid):return execute(self.repo,rid,self.native.rpc,self.cancel)

    def test_native_handshake_complete_and_no_business_publication(self):
        rid=self.repo.new_run()['id'];result=self.run_it(rid)
        self.assertEqual(result['state'],'COMPLETE')
        self.assertEqual(self.native.ack['purpose'],'stockout_analysis')
        self.assertFalse((self.repo.root/'warehouse_catalogs').exists())
        self.assertFalse((self.repo.root/'workbench.sqlite3').exists())
        self.assertEqual(len([c for c in self.native.calls if c[1].get('action')=='sync.provide_observation']),1)
        jobs=Jobs(self.repo.root/'aoxiang/jobs.sqlite3')
        self.assertEqual(len(jobs.recent()),1);self.assertEqual(jobs.recent(exclude_analysis=True),[])
        with self.assertRaisesRegex(ValueError,'分析'):jobs.maintenance_preview(None,jobs.recent()[0]['id'],'end')

    def test_cancel_then_resume_keeps_native_command_and_catalog(self):
        rid=self.repo.new_run()['id'];self.native.pause_on_reserve=True
        with self.assertRaises(Cancelled):self.run_it(rid)
        self.assertEqual(self.repo.run(rid)['state'],'PAUSED')
        external=self.repo.run(rid)['external'];self.cancel.clear();self.native.pause_on_reserve=False
        self.assertEqual(self.run_it(rid)['state'],'COMPLETE')
        self.assertEqual(len(self.native.commands),1)
        self.assertEqual(self.repo.run(rid)['external']['ax_commands'],external['ax_commands'])
        self.assertEqual(len([c for c in self.native.calls if c[1].get('action')=='sync.start']),1)

    def test_unknown_accepted_response_reconciles_same_id(self):
        rid=self.repo.new_run()['id'];self.native.unknown_once=True
        with self.assertRaises(ValueError):self.run_it(rid)
        self.assertEqual(self.repo.run(rid)['state'],'REVIEW')
        original=list(self.native.commands)
        self.assertEqual(self.run_it(rid)['state'],'COMPLETE')
        self.assertEqual(list(self.native.commands),original)

    def test_identity_change_blocks_before_new_export(self):
        rid=self.repo.new_run()['id'];self.native.pause_on_reserve=True
        with self.assertRaises(Cancelled):self.run_it(rid)
        self.cancel.clear();self.native.mapping='changed'
        with self.assertRaisesRegex(ValueError,'绑定已变更'):self.run_it(rid)
        self.assertEqual(len(self.native.commands),1)

    def test_analysis_receipt_cannot_adopt_business(self):
        from catalog_sync import Adoptions
        adoption=Adoptions.__new__(Adoptions)
        with self.assertRaisesRegex(ValueError,'分析资料'):adoption.apply({'purpose':'stockout_analysis'}, {})


class ScheduleTests(unittest.TestCase):
    def test_default_does_not_create_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)/'missing';self.assertEqual(trigger.load(folder),{'enabled':False});self.assertFalse(folder.exists())

    def test_timezone_once_and_missed_history(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            trigger.save(folder,True,'08:00',datetime(2026,11,7,7,0,tzinfo=TZ))
            self.assertIsNone(trigger.due(folder,datetime(2026,11,7,7,59,tzinfo=TZ)))
            self.assertEqual(trigger.due(folder,datetime(2026,11,7,8,0,tzinfo=TZ)),{'trigger':'schedule:2026-11-07'})
            self.assertIsNone(trigger.due(folder,datetime(2026,11,7,9,0,tzinfo=TZ)))
            self.assertTrue(trigger.due(folder,datetime(2026,11,10,9,0,tzinfo=TZ))['missed'])
            self.assertFalse((folder/'analysis/stockout/stockout.sqlite3').exists())

    def test_bad_and_oversized_markers_do_not_enable_work(self):
        with tempfile.TemporaryDirectory() as temp:
            target=trigger.path(temp);target.parent.mkdir();target.write_text('x'*9000)
            with self.assertRaises(ValueError):trigger.load(temp)


if __name__=='__main__':unittest.main()
