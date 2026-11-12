"""Isolated combined capture, replay, scope and accounting contracts."""
import os,sys,tempfile,threading,unittest,copy
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from tests.test_stockout_acquisition import NativeFixture
from tests.test_stockout_analysis import workbook
from stockout_repository import Repository as Store
from stockout_hub_service import Journal,preview,create,execute
import warehouse_stockout as warehouse
from runtime_support import Cancelled


class HubTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.folder=self.root/'settings'
        self.repo=Store(self.folder);self.repo.initialize();self.repo.save_settings({'stores':[{'code':'S','name':'测试门店'}],'routes':{'S':{'primary':'山东仓','secondary':None}},'profile':None})
        self.journal=Journal(self.folder);self.cancel=threading.Event();self.native=NativeFixture(self.root,self.cancel)
        for name in ('stockout_hub_service.today','stockout_repository.today','stockout_acquisition.today'):
            p=patch(name,return_value='2026-11-07');p.start();self.addCleanup(p.stop)
    def start(self,mode='both'):
        return create(self.journal,preview(self.folder,mode,['山东仓']))['id']
    def run_it(self,rid):return execute(self.journal,rid,self.native.rpc,self.cancel)
    def test_combined_once_and_idempotent_completed_replay(self):
        rid=self.start();row=self.run_it(rid);self.assertEqual(row['state'],'COMPLETE',row['errors'])
        self.assertEqual(self.repo.run(row['store_run'])['state'],'COMPLETE')
        wh=warehouse.Repository(self.folder);self.assertEqual(wh.run(row['warehouse_run'])['state'],'COMPLETE')
        self.assertEqual(len([p for s,p in self.native.calls if s=='jst' and p.get('action')=='sync.start']),1)
        calls=len(self.native.calls);self.assertEqual(self.run_it(rid),row);self.assertEqual(len(self.native.calls),calls)
        self.assertEqual(self.repo.run(row['store_run'])['catalogs'],row['catalogs'])
        self.assertTrue(self.native.ack);self.assertEqual(self.native.ack['run_id'],rid)
    def test_warehouse_only_never_calls_ax_or_creates_store_child(self):
        rid=self.start('warehouse');row=self.run_it(rid);self.assertEqual(row['state'],'COMPLETE')
        self.assertIsNone(row['store_run']);self.assertFalse(any(s=='ax' for s,p in self.native.calls))
        with self.repo.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM runs').fetchone()[0],0)
    def test_store_only_does_not_create_warehouse_result(self):
        row=self.run_it(self.start('store'));self.assertEqual(row['state'],'COMPLETE')
        self.assertFalse(warehouse.Repository(self.folder).path.exists());self.assertIsNone(row['warehouse_run'])
    def test_unknown_ax_response_preserves_published_warehouse_and_same_native_ids(self):
        rid=self.start();self.native.unknown_once=True;first=self.run_it(rid);self.assertEqual(first['state'],'PARTIAL')
        wh=warehouse.Repository(self.folder)
        with wh.connect() as db:before=[tuple(r) for r in db.execute('SELECT * FROM current')]
        cid=list(self.native.commands);operation=self.native.operation
        second=self.run_it(rid);self.assertEqual(second['state'],'COMPLETE',second['errors']);self.assertEqual(list(self.native.commands),cid);self.assertEqual(self.native.operation,operation)
        with wh.connect() as db:self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM current')],before)
    def test_cancel_after_native_acceptance_continues_original(self):
        rid=self.start();self.native.pause_on_reserve=True
        with self.assertRaises(Cancelled):self.run_it(rid)
        operation=self.native.operation;cid=list(self.native.commands)
        self.cancel.clear();self.native.pause_on_reserve=False
        self.assertEqual(self.run_it(rid)['state'],'COMPLETE');self.assertEqual(self.native.operation,operation);self.assertEqual(list(self.native.commands),cid)
    def test_stale_preview_and_cross_day_do_not_dispatch(self):
        plan=preview(self.folder,'both',['山东仓']);self.repo.save_settings({'stores':[{'code':'T','name':'另店'}],'routes':{},'profile':None})
        with self.assertRaisesRegex(ValueError,'变化'):create(self.journal,plan)
        rid=self.start()
        with patch('stockout_hub_service.today',return_value='2026-11-08'):
            with self.assertRaisesRegex(ValueError,'跨日'):self.run_it(rid)
        self.assertFalse(self.native.calls)
    def test_offline_reuse_keeps_observation_and_can_delete_independently(self):
        from stockout_hub_reuse import reuse
        rid=self.start('store');row=self.run_it(rid);calls=len(self.native.calls)
        result=reuse(self.journal,rid,self.cancel,lambda _:None);self.assertEqual(len(self.native.calls),calls)
        wh=warehouse.Repository(self.folder)
        with wh.connect() as db:self.assertEqual(db.execute('SELECT DISTINCT day FROM snapshots').fetchone()[0],'2026-11-07')
        from warehouse_stockout_maintenance import preview as delete_plan,remove
        remove(wh,delete_plan(wh,'2026-11-07','2026-11-07'),self.cancel)
        with self.assertRaisesRegex(ValueError,'删除'):reuse(self.journal,rid,self.cancel,lambda _:None)
        self.assertEqual(self.repo.run(row['store_run'])['state'],'COMPLETE')
    def test_shared_warehouse_raw_and_combined_export_numeric_values(self):
        from stockout_hub_exports import export
        from stockout_views import defaults
        from warehouse_stockout_views import defaults as wd
        self.run_it(self.start());target=self.root/'combined.xlsx'
        export(self.folder,{'mode':'both','destination':str(target),'store_query':{'start':'2026-11-07'},'warehouse_query':{'start':'2026-11-07','end':'2026-11-07'},'store_combinations':defaults()['combinations'],'warehouse_combinations':wd()['combinations'],'include_detail':True},self.cancel,lambda _:None)
        import openpyxl
        with target.open('rb') as f:
            book=openpyxl.load_workbook(f,read_only=True,data_only=True)
            try:
                self.assertTrue(any(n.startswith('门店_') for n in book.sheetnames));self.assertTrue(any(n.startswith('大仓_') for n in book.sheetnames))
                self.assertEqual(book['大仓_范围汇总']['B2'].value,1)
                self.assertIsInstance(book['门店_门店总览']['E2'].value,(int,float))
            finally:book.close()

    def test_two_warehouses_one_batch_and_independent_partial_scope(self):
        self.native=MultiNative(self.root/'multi',self.cancel);self.native.missing={'河北仓'}
        rid=create(self.journal,preview(self.folder,'both',['山东仓','河北仓']))['id']
        first=self.run_it(rid);self.assertEqual(first['state'],'PARTIAL')
        self.assertEqual(self.repo.run(first['store_run'])['state'],'COMPLETE')
        self.assertEqual(self.native.start_items,['山东仓','河北仓'])
        wh=warehouse.Repository(self.folder)
        with wh.connect() as db:self.assertEqual(db.execute('SELECT warehouse FROM current').fetchall()[0][0],'山东仓')
        self.native.missing.clear();second=self.run_it(rid);self.assertEqual(second['state'],'COMPLETE',second['errors'])
        self.assertEqual(len(self.native.commands),1)
        with wh.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM snapshots').fetchone()[0],2)
        self.assertEqual(len(self.native.captures),1)

    def test_consumer_saved_before_capture_ack_replays_without_duplicate_snapshot(self):
        rid=self.start();original=self.journal.change;interrupted=[False]
        def change(key,**values):
            if 'catalogs' in values and not interrupted[0]:interrupted[0]=True;raise RuntimeError('模拟消费者保存后断电')
            return original(key,**values)
        with patch.object(self.journal,'change',side_effect=change):self.assertEqual(self.run_it(rid)['state'],'PARTIAL')
        self.assertEqual(self.run_it(rid)['state'],'COMPLETE')
        wh=warehouse.Repository(self.folder)
        with wh.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM snapshots').fetchone()[0],1)

    def test_schedule_replay_uses_same_intention(self):
        plan=preview(self.folder,'warehouse',['山东仓']);first=create(self.journal,plan,trigger='schedule:2026-11-07:08:00:warehouse')
        self.run_it(first['id']);second=create(self.journal,plan,trigger=first['trigger'])
        self.assertEqual(first['id'],second['id']);self.assertEqual(len(self.journal.recent()),1)

    def test_finished_warehouse_not_blocked_by_unfinished_ax(self):
        rid=self.start();self.native.unknown_once=True;first=self.run_it(rid)
        self.assertEqual(first['state'],'PARTIAL')
        later=create(self.journal,preview(self.folder,'warehouse',['山东仓']))
        self.assertNotEqual(later['id'],rid)

    def test_resume_never_rewinds_newer_independent_warehouse_snapshot(self):
        self.native=MultiNative(self.root/'multi',self.cancel);self.native.missing={'河北仓'}
        rid=create(self.journal,preview(self.folder,'both',['山东仓','河北仓']))['id'];self.run_it(rid)
        wh=warehouse.Repository(self.folder);new=wh.new_run(['山东仓'],day='2026-11-07')
        warehouse.load_master(wh,self.native.receipts['山东仓']['source'],'山东仓',run=new,observed='2026-11-07T10:00:00+08:00');wh.publish(new,'山东仓')
        with wh.connect() as db:before=tuple(db.execute("SELECT * FROM current WHERE warehouse='山东仓'").fetchone())
        self.native.missing.clear();self.assertEqual(self.run_it(rid)['state'],'COMPLETE')
        with wh.connect() as db:self.assertEqual(tuple(db.execute("SELECT * FROM current WHERE warehouse='山东仓'").fetchone()),before)

    def test_resume_after_store_recalculation_does_not_replace_current_revision(self):
        self.native.codes=['S','T'];self.native.fail_codes={'T'}
        self.repo.save_settings({'stores':[{'code':c,'name':c} for c in ['S','T']],'routes':{c:{'primary':'山东仓','secondary':None} for c in ['S','T']},'profile':None})
        rid=self.start();first=self.run_it(rid);self.assertEqual(first['state'],'PARTIAL')
        from stockout_lifecycle import preview as action_preview,recalculate
        plan=action_preview(self.repo,first['store_run'],'recalculate','original');recalculate(self.repo,plan,cancel=self.cancel)
        with self.repo.connect() as db:before=tuple(db.execute('SELECT * FROM current_days').fetchone())
        with self.assertRaisesRegex(ValueError,'日报已更新'):self.run_it(rid)
        with self.repo.connect() as db:self.assertEqual(tuple(db.execute('SELECT * FROM current_days').fetchone()),before)


class MultiNative(NativeFixture):
    def __init__(self,root,cancel):
        super().__init__(root,cancel);self.observations={};self.acks={};self.receipts={};self.missing=set();self.captures=set();self.start_items=[]
        from catalog_sync_validation import validate
        from stockout_common import file_hash
        for code,name in [('sd','山东仓'),('hb','河北仓')]:
            folder=root/'jst/downloads/batch'/code;folder.mkdir(parents=True)
            path=workbook(folder/'source.xlsx',['商品编码','分类','基本售价','出库箱规','商品状态','可用数','供应商名称','虚拟分类','商品标签'],[['A','食品',1,1,'启用',0,'供应商','',''],['B','食品',1,1,'启用',0,'供应商','淘便考核','']])
            self.receipts[name]=validate({'schema':1,'file':str(path),'warehouse':name,'sha256':file_hash(path),'expected_rows':2,'candidate':str(folder/'candidate.json')})
    def rpc(self,service,**payload):
        if service!='jst':return super().rpc(service,**payload)
        self.calls.append((service,copy.deepcopy(payload)));action=payload['action'];v=payload['values']
        if action=='sync.start':
            self.start_items=[i['warehouse'] for i in v['items']];self.captures.add(v['operation_id']);self.operation=v['operation_id'];return {'batch_id':'batch','reused':len([p for s,p in self.calls if p.get('action')=='sync.start'])>1}
        if action=='sync.status':
            items=[]
            for code,name in [('sd','山东仓'),('hb','河北仓')]:
                if name not in self.start_items:continue
                item={'id':code,'account':{'warehouse':name,'expectedCoid':'C'+code,'expectedUid':'U'+code},'stage':'READY' if code in self.observations else 'WAIT_HOST','observationRequest':'token'+code,'observation':self.observations.get(code),'native':{'scope':{'coid':'C'+code,'uid':'U'+code},'sha256':self.receipts[name]['source_sha256']},'receipt':self.receipts[name] if code in self.observations and name not in self.missing else None,'adoption':self.acks.get(code)}
                items.append(item)
            return {'running':any(i['stage']=='WAIT_HOST' for i in items),'accounts':[i['account'] for i in items],'batches':[{'id':'batch','operation_id':self.operation,'purpose':'stockout_analysis','items':items}]}
        if action=='sync.provide_observation':self.observations[v['item_id']]=v['observation'];return {}
        if action=='sync.acknowledge_adoption':self.acks[v['item_id']]=v['receipt'];return {}
        if action in ('sync.resume','sync.stop'):return {}
        raise AssertionError(action)


if __name__=='__main__':unittest.main()
