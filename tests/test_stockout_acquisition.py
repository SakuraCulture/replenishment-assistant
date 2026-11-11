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
        self.codes=['S'];self.fail_codes=set();self.bad_store_codes=set();self.results={};self.before_reserve=None;self.reexports={}
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
                return {'running':not bool(self.observation),'accounts':[copy.deepcopy(item['account'])],'batches':[{'id':'batch','operation_id':self.operation,'purpose':'stockout_analysis','items':[item]}]}
            if action=='sync.provide_observation':
                assert self.observation is None;assert v['token']=='token';self.observation=v['observation'];return {'accepted':True}
            if action=='sync.acknowledge_adoption':self.ack=v['receipt'];return {'accepted':True}
            if action=='sync.stop':self.observation=self.observation or {'stopped':True};return {}
            if action=='sync.resume':return {}
            raise AssertionError(action)
        if action=='configuration':return {'configured':True,'enabled':True,'account_binding':{'account_id':'A','merchant_code':'M'},
            'store_mapping_version':self.mapping,'targets':[{'buyerWarehouseCode':code,'displayName':'测试门店'+code} for code in self.codes]}
        if action=='collection.reexport.preview':
            return {'id':v['operation_id'],'action':'reexport','token':'a'*64,'stores':[{'code':c,'name':'测试门店'+c} for c in v['warehouse_codes']],
                    'purpose':v['purpose'],'blocked':[],'related':[],'retained':[]}
        if action=='collection.reexport.apply':
            if v['operation_id'] in self.reexports:return self.reexports[v['operation_id']]
            records=[]
            for row in self.commands.values():
                if row['request'].get('consumer',{}).get('purpose')!=v['purpose']:continue
                touched=False
                for i in row['items']:
                    if i['warehouse_code'] in v['warehouse_codes'] and i['state'] not in {'SUCCEEDED','ENDED'}:
                        i['state']='ENDED';touched=True
                if touched:
                    row['sequence']+=1
                    if all(i['state'] in {'SUCCEEDED','ENDED'} for i in row['items']):row['state']='ENDED'
                    records.append(copy.deepcopy(row))
            receipt={'id':v['operation_id'],'records':records};self.reexports[v['operation_id']]=receipt;return receipt
        if action=='reserve':
            command=v['command'];cid=command['command_id']
            if self.before_reserve:self.before_reserve(command)
            if cid not in self.commands:
                folder=self.root/'ax/command-files'/cid;folder.mkdir(parents=True)
                items=[]
                for index,code in enumerate(command['scope']['warehouse_codes']):
                    raw_code='WRONG' if code in self.bad_store_codes else code
                    source=workbook(folder/'source.xlsx',['仓库/门店编码','商品编码','商品名称','月销','可用库存','在途库存'],
                                    [[raw_code,'A','商品A',1,0,6],[raw_code,'B','商品B',1,2,0]])
                    sha=file_hash(source);destination=folder/(str(index)+'-'+sha+'.xlsx');source.rename(destination)
                    native='native-'+cid+'-'+code
                    result={'warehouse_code':code,'run_id':native,'file':str(destination.resolve()),'sha256':sha,'bytes':destination.stat().st_size,
                            'rows':2,'account_binding':command['account_binding'],'collected_at':'2026-11-07T08:00:00+08:00'}
                    self.results[(cid,code)]=result
                    failed=code in self.fail_codes
                    items.append({'index':index,'warehouse_code':code,'run_id':native,'task_id':'accepted-'+native,
                                  'state':'FAILED' if failed else 'SUCCEEDED','result':None if failed else result,
                                  'error':{'code':'QUERY_FILE_COUNT_DIFF','message':'原件条数待核对'} if failed else None})
                self.commands[cid]={'id':cid,'fingerprint':fingerprint(command),'request':command,'sequence':1,'state':'SUCCEEDED',
                    'items':items}
                if any(i['state']=='FAILED' for i in items):self.commands[cid]['state']='COMPLETED_WITH_ERRORS'
                if self.pause_on_reserve:self.commands[cid]['state']='RUNNING';self.cancel.set()
                if self.unknown_once:self.unknown_once=False;raise RuntimeError('受理响应中断，结果未知')
            return {'record':copy.deepcopy(self.commands[cid])}
        if action=='status':return copy.deepcopy(self.commands[v['command_id']])
        if action in {'resume','pause'}:
            row=self.commands[v['command_id']];row['sequence']+=1;row['state']='PAUSED' if action=='pause' else 'SUCCEEDED';return copy.deepcopy(row)
        if action=='retry':
            row=self.commands[v['command_id']];assert row['sequence']==v['sequence']
            for index in v['indices']:
                item=row['items'][index];assert item['state']=='FAILED'
                item.update(state='SUCCEEDED',error=None,result=self.results[(row['id'],item['warehouse_code'])])
            row['sequence']+=1;row['state']='SUCCEEDED';return copy.deepcopy(row)
        raise AssertionError(action)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.repo=Repository(self.root/'settings');self.repo.initialize()
        self.repo.save_settings({'stores':[{'code':'S','name':'测试门店'}],'routes':{'S':{'primary':'山东仓','secondary':None}},'profile':None})
        self.cancel=threading.Event();self.native=NativeFixture(self.root,self.cancel)

    def run_it(self,rid):return execute(self.repo,rid,self.native.rpc,self.cancel)

    def select(self,codes):
        self.native.codes=list(codes)
        self.repo.save_settings({'stores':[{'code':c,'name':'测试门店'+c} for c in codes],
                                'routes':{c:{'primary':'山东仓','secondary':None} for c in codes},'profile':None})

    def test_all_stores_share_one_ordered_command_linked_before_dispatch(self):
        self.select(['T','S','U']);rid=self.repo.new_run()['id']
        def linked(command):
            self.assertEqual(self.repo.run(rid)['external']['ax_commands'],{c:command['command_id'] for c in ['T','S','U']})
        self.native.before_reserve=linked
        result=self.run_it(rid)
        self.assertEqual(result['state'],'COMPLETE');self.assertEqual(len(self.native.commands),1)
        job=Jobs(self.repo.root/'aoxiang/jobs.sqlite3').recent()[0]
        self.assertEqual(job['command']['scope']['warehouse_codes'],['T','S','U'])
        self.assertEqual(job['context']['purpose'],'stockout_analysis')
        self.assertEqual([s['store'] for s in result['stores']],['T','S','U'])
        self.assertTrue(all(s['state']=='READY' for s in result['stores']))

    def test_partial_batch_revision_retries_only_failed_store_same_command(self):
        self.select(['S','T','U']);self.native.fail_codes={'T'};rid=self.repo.new_run()['id']
        first=self.run_it(rid);self.assertEqual(first['state'],'PARTIAL')
        self.assertEqual([s['state'] for s in first['stores']],['READY','FAILED','READY'])
        cid=next(iter(self.native.commands));original=copy.deepcopy(self.native.commands[cid]['items'][0])
        revision=self.repo.new_run(source_run=rid)['id'];last=self.run_it(revision)
        self.assertEqual(last['state'],'COMPLETE');self.assertEqual(len(self.native.commands),1)
        retries=[p['values'] for s,p in self.native.calls if p.get('action')=='retry']
        self.assertEqual([r['indices'] for r in retries],[[1]])
        self.assertEqual(self.native.commands[cid]['items'][0],original)
        self.assertEqual(self.repo.run(rid),first)

    def test_multistore_cancel_keeps_batch_identity(self):
        self.select(['S','T','U']);rid=self.repo.new_run()['id'];self.native.pause_on_reserve=True
        with self.assertRaises(Cancelled):self.run_it(rid)
        command=copy.deepcopy(next(iter(self.native.commands.values()))['request'])
        self.cancel.clear();self.native.pause_on_reserve=False
        self.assertEqual(self.run_it(rid)['state'],'COMPLETE')
        self.assertEqual(next(iter(self.native.commands.values()))['request'],command)
        self.assertEqual(len(self.native.commands),1)
    def test_multistore_unknown_response_reconciles_same_batch(self):
        self.select(['S','T','U']);rid=self.repo.new_run()['id'];self.native.unknown_once=True
        with self.assertRaises(ValueError):self.run_it(rid)
        ids=list(self.native.commands)
        self.assertEqual(self.run_it(rid)['state'],'COMPLETE');self.assertEqual(list(self.native.commands),ids)
        self.assertEqual(len(ids),1)

    def test_cancel_during_projection_preserves_ready_store_and_original_files(self):
        from unittest.mock import patch
        from stockout_facts import load_store
        self.select(['S','T','U']);rid=self.repo.new_run()['id'];seen=[]
        def project(*args,**kw):
            seen.append(args[2]);result=load_store(*args,**kw)
            if seen==['S']:self.cancel.set()
            return result
        with patch('stockout_acquisition.load_store',project):
            with self.assertRaises(Cancelled):self.run_it(rid)
            ready=self.repo.run(rid)['stores'][0]['snapshot']
            self.cancel.clear();self.assertEqual(self.run_it(rid)['state'],'COMPLETE')
        self.assertEqual(seen,['S','T','U']);self.assertEqual(self.repo.run(rid)['stores'][0]['snapshot'],ready)
        self.assertEqual(len([p for s,p in self.native.calls if p.get('action')=='reserve']),1)

    def test_existing_single_store_commands_resume_without_merging_or_recreating(self):
        from aoxiang_acquisition import prepare
        self.select(['S','T','U']);rid=self.repo.new_run()['id'];jobs=Jobs(self.repo.root/'aoxiang/jobs.sqlite3')
        config=self.native.rpc('ax',action='configuration',values={});commands={}
        for code in ['S','T']:
            commands[code]=prepare(jobs,config,[code],'refresh',day='2026-11-07',purpose='stockout_analysis',owner=rid)['id']
        self.repo.change(rid,external={'ax_commands':commands})
        result=self.run_it(rid);self.assertEqual(result['state'],'COMPLETE')
        for code,cid in commands.items():
            self.assertEqual(result['external']['ax_commands'][code],cid)
            self.assertEqual(self.native.commands[cid]['request']['scope']['warehouse_codes'],[code])
        self.assertEqual(len(self.native.commands),3)

    def test_business_pending_stays_untouched_and_does_not_lock_analysis(self):
        from aoxiang_acquisition import prepare
        self.select(['S','T','U']);rid=self.repo.new_run()['id'];jobs=Jobs(self.repo.root/'aoxiang/jobs.sqlite3')
        config=self.native.rpc('ax',action='configuration',values={})
        business=prepare(jobs,config,['T'],'refresh',day='2026-11-07')
        result=self.run_it(rid);self.assertEqual(result['state'],'COMPLETE')
        self.assertEqual([s['state'] for s in result['stores']],['READY','READY','READY'])
        self.assertEqual(jobs.get(business['id']),business)
        self.assertEqual(next(iter(self.native.commands.values()))['request']['scope']['warehouse_codes'],['S','T','U'])

    def test_selected_analysis_reexport_creates_revision_and_keeps_other_ready_snapshot(self):
        from stockout_acquisition import reexport_preview,reexport
        from unittest.mock import patch
        self.select(['S','T']);self.native.fail_codes={'T'}
        old=self.run_it(self.repo.new_run(day='2026-11-07')['id']);self.assertEqual(old['state'],'PARTIAL')
        snapshot=old['stores'][0]['snapshot'];old_id=old['id'];commands=copy.deepcopy(old['external']['ax_commands'])
        with patch('stockout_acquisition.today',return_value='2026-11-07'),patch('stockout_repository.today',return_value='2026-11-07'):
            plan=reexport_preview(self.repo,old_id,['T'],self.native.rpc,self.cancel,lambda _:None)
            self.native.fail_codes.clear();new=reexport(self.repo,plan,self.native.rpc,self.cancel,lambda _:None)
        self.assertEqual(new['state'],'COMPLETE');self.assertNotEqual(new['id'],old_id)
        self.assertEqual(new['stores'][0]['snapshot'],snapshot)
        self.assertEqual(self.repo.run(old_id)['stores'][0]['snapshot'],snapshot)
        self.assertEqual(new['external']['ax_commands']['S'],commands['S'])
        submissions=[p['values']['command']['scope']['warehouse_codes'] for s,p in self.native.calls if s=='ax' and p.get('action')=='reserve']
        self.assertEqual(submissions,[['S','T'],['T']])
        self.assertNotEqual(new['external']['ax_commands']['T'],commands['T'])

    def test_reexport_preview_becomes_stale_when_current_report_changes_before_apply(self):
        from stockout_acquisition import reexport_preview,reexport
        from unittest.mock import patch
        self.select(['S','T']);self.native.fail_codes={'T'}
        old=self.run_it(self.repo.new_run(day='2026-11-07')['id'])
        with patch('stockout_acquisition.today',return_value='2026-11-07'):
            plan=reexport_preview(self.repo,old['id'],['T'],self.native.rpc,self.cancel,lambda _:None)
            newer=self.repo.new_run(source_run=old['id'])
            with self.repo.connect(write=True) as db:
                cursor=db.execute('INSERT INTO publications(day,run,created) VALUES(?,?,?)',('2026-11-07',newer['id'],'synthetic-new-pointer'))
                db.execute('UPDATE current_days SET publication=? WHERE day=?',(cursor.lastrowid,'2026-11-07'))
            before=len(self.native.calls)
            with self.assertRaisesRegex(ValueError,'日报已变化'):reexport(self.repo,plan,self.native.rpc,self.cancel,lambda _:None)
            self.assertEqual(len(self.native.calls),before)

    def test_bad_store_data_does_not_discard_other_complete_files(self):
        self.select(['S','T','U']);self.native.bad_store_codes={'T'};rid=self.repo.new_run()['id']
        result=self.run_it(rid);self.assertEqual(result['state'],'PARTIAL')
        self.assertEqual([s['state'] for s in result['stores']],['READY','FAILED','READY'])
        self.assertIn('门店',result['stores'][1]['error']);self.assertEqual(len(self.native.commands),1)

    def test_changed_original_still_rejects_manifest_without_adoption(self):
        self.select(['S','T']);rid=self.repo.new_run()['id'];self.native.pause_on_reserve=True
        with self.assertRaises(Cancelled):self.run_it(rid)
        row=next(iter(self.native.commands.values()))
        Path(row['items'][1]['result']['file']).write_bytes(b'changed')
        self.cancel.clear();self.native.pause_on_reserve=False
        with self.assertRaisesRegex(ValueError,'没有完整验收'):self.run_it(rid)
        self.assertTrue(all(s['snapshot'] is None for s in self.repo.run(rid)['stores']))
        self.assertEqual(len(self.native.commands),1)

    def test_incomplete_batch_association_cannot_dispatch_broader_scope(self):
        from aoxiang_acquisition import prepare
        self.select(['S','T']);rid=self.repo.new_run()['id'];jobs=Jobs(self.repo.root/'aoxiang/jobs.sqlite3')
        config=self.native.rpc('ax',action='configuration',values={})
        job=prepare(jobs,config,['S'],'refresh',day='2026-11-07',purpose='stockout_analysis',owner=rid)
        self.repo.change(rid,external={'ax_commands':{'S':job['id'],'T':job['id']}})
        with self.assertRaises(ValueError):self.run_it(rid)
        self.assertEqual(self.native.commands,{})

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
