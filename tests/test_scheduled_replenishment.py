"""Portable scheduler/recovery/equivalence acceptance. Synthetic data, real core/spawn."""
import copy,sys,tempfile,threading,unittest,sqlite3
from pathlib import Path
from datetime import datetime,timedelta
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import scheduled_replenishment as sr
import scheduled_execution as se
import warehouse_catalog as wc
import work_repository as wr
import store_parameter_import as spi
import replenishment_plans as plans
import batch_service
from openpyxl import Workbook


def fixture(root):
    work=wr.Repository(root);catalogs={};files=[]
    for wh in ('山东仓','河北仓'):
        p=root/(wh+'.xlsx');b=Workbook();w=b.active;w.append(wc.REQUIRED+['商品标签','商品名称'])
        w.append(['0001','饮品',2,6,'启用',100,'必卖清单' if wh=='河北仓' else '','合成饮品'])
        w.append(['0002','饮品',3,1,'启用',100,'','合成单件']);b.save(p);b.close()
        catalogs[wh]=wc.import_catalog(p,wh);wc.Repository(root).save(catalogs[wh])
    for code in ('A','B'):
        r=spi.base();r['ui'].update(mode=1,primary='河北仓' if code=='A' else '山东仓',secondary='山东仓' if code=='A' else None)
        work.save_stores({code:r});p=root/(code+'.xlsx');b=Workbook();w=b.active
        w.append(['仓库/门店编码','仓库/门店名称','商品编码','商品名称','日销','周销','月销','可用库存','在途库存','缺货天数（天）','补货状态'])
        for sku in ('0001','0002'):w.append([code,'测试'+code,sku,'合成'+sku,2,14,60,0,0,0,'正常'])
        b.save(p);b.close();files.append({'file':str(p),'warehouse_code':code,'sha256':batch_service.engine.adapter.digest(p)})
    plan=plans.Repository(work.path).save({'name':'合成预案','stores':[{'code':c,'name':'测试'+c} for c in ('A','B')],'config':spi.base()['preset']['config'],'parameter_fields':[]})
    s={'name':'早班','plan_id':plan['id'],'stores':plan['stores'],'time':'06:30','kind':'weekly','weekdays':list(range(7)),'workers':2,'enabled':False}
    basis=se.freeze(root,s,'2026-11-04');s['check_signature']=se.check_signature(basis)
    return work,s,basis,catalogs,{'files':files}


class LocalBackend:
    def __init__(self,catalogs,manifest,cancel=None):self.data=catalogs;self.manifest=manifest;self.cancel=cancel;self.calls=[]
    def acquire(self):self.calls.append('acquire')
    def sales(self,runner,basis):self.calls.append('sales');return self.manifest
    def catalogs(self,runner,basis):
        self.calls.append('catalogs')
        for wh,c in self.data.items():runner.repo.save_value(runner.rid,'catalog:'+wh,c,runner.epoch)
        if self.cancel:self.cancel.set()
        return self.data
    def release(self,p):self.calls.append('release')


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.work=wr.Repository(self.root);self.now=datetime(2026,11,4,6,29,tzinfo=sr.TZ)
        self.repo=sr.Repository(self.work.path,lambda:self.now)
        self.s={'name':'早班','plan_id':'p','time':'06:30','kind':'weekly','weekdays':[2],'workers':2,'enabled':True,'check_signature':'verified'}
    def tearDown(self):self.tmp.cleanup()
    def test_busy_commit_keeps_original_transaction_and_one_registration(self):
        repo=self.repo;repo.ensure();opened=threading.Event();committing=threading.Event()
        with repo.db() as db:
            db.execute('BEGIN');db.execute('SELECT id FROM scheduled_runs').fetchall()
            outcomes=[]
            def write():
                try:
                    with repo.db(True) as transaction:
                        transaction.execute("INSERT INTO scheduled_arrangements VALUES('busy-probe','{}')")
                        committing.set()
                    outcomes.append('saved')
                except Exception as exc:outcomes.append(exc)
                finally:opened.set()
            writer=threading.Thread(target=write);writer.start()
            self.assertTrue(committing.wait(2))
            self.assertFalse(opened.wait(1.2))
            db.commit()
        writer.join(5)
        self.assertEqual(outcomes,['saved'])
        with repo.db() as db:self.assertEqual(db.execute("SELECT count(*) FROM scheduled_arrangements WHERE id='busy-probe'").fetchone()[0],1)
    def test_once_trigger_and_no_duplicate_after_restart(self):
        self.repo.save_schedule(self.s);self.now+=timedelta(minutes=1)
        r=self.repo.due();self.assertEqual(len(r),1);self.assertEqual(r[0]['day'],'2026-11-04');self.assertEqual(self.repo.due(),[])
        self.assertEqual(sr.Repository(self.work.path,lambda:self.now).due(),[])
    def test_missed_once_and_weekly_no_backlog(self):
        self.repo.save_schedule(self.s);self.now+=timedelta(days=20)
        r=self.repo.due();self.assertEqual(len(r),1);self.assertEqual(r[0]['state'],'MISSED');self.assertEqual(self.repo.due(),[])
    def test_exact_60_second_grace(self):
        self.repo.save_schedule(self.s);self.now+=timedelta(seconds=120)
        self.assertEqual(self.repo.due()[0]['state'],'QUEUED')
    def test_pause_disable_not_same_action(self):
        s=self.repo.save_schedule(self.s);r=self.repo.manual(s);r=self.repo.claim(r['id'])
        self.repo.request_pause(r['id']);self.assertTrue(self.repo.schedules()[0]['enabled'])
        self.repo.save_schedule({**s,'enabled':False},s);self.assertEqual(self.repo.get(r['id'])['state'],'PAUSING')
    def test_revision_and_epoch_guards(self):
        r=self.repo.manual(self.s);running=self.repo.claim(r['id']);self.repo.recover()
        with self.assertRaises(wr.Conflict):self.repo.change(r['id'],epoch=running['epoch'],state='COMPLETE')
        with self.assertRaises(wr.Conflict):self.repo.resume(r['id'],running['revision'])
    def test_process_restart_recovers_only_active_same_day_originals(self):
        old=self.repo.claim(self.repo.manual(self.s)['id'])
        self.now+=timedelta(days=1)
        active=self.repo.claim(self.repo.manual(self.s)['id'])
        self.repo.change(active['id'],epoch=active['epoch'],external={'ax':'original'},retry_codes=['B'],retry_id='selected')
        self.repo.save_value(active['id'],'checkpoint',{'store':'A','quantity':12},active['epoch'])
        paused=self.repo.claim(self.repo.manual(self.s)['id']);self.repo.request_pause(paused['id'])
        ended=self.repo.claim(self.repo.manual(self.s)['id']);self.repo.request_pause(ended['id'],True)
        self.repo.recover(after_process_exit=True)
        recovered=self.repo.get(active['id'])
        self.assertEqual(recovered['state'],'QUEUED')
        self.assertEqual((recovered['external'],recovered['retry_codes'],recovered['retry_id']),({'ax':'original'},['B'],'selected'))
        self.assertTrue(recovered['recovering_process'])
        self.assertEqual(self.repo.value(active['id'],'checkpoint'),{'store':'A','quantity':12})
        self.assertEqual([self.repo.get(run['id'])['state'] for run in (old,paused,ended)],['PAUSED','PAUSED','ENDED'])
        self.assertIn('跨业务日',self.repo.get(old['id'])['message'])
        self.repo.recover(after_process_exit=True)
        self.assertEqual(recovered,self.repo.get(active['id']))

    def test_process_restart_does_not_reissue_a_completed_failed_export(self):
        from scheduled_platforms import Platforms
        from unittest.mock import Mock
        client,jobs=Mock(),Mock()
        job={'id':'original','state':'COMPLETED_WITH_ERRORS','sequence':2,
             'record':{'items':[{'index':0,'state':'FAILED','warehouse_code':'A'}]}}
        self.assertFalse(Platforms.continue_sales(client,jobs,job,None,None,after_process_exit=True))
        client.request.assert_not_called()
        self.assertTrue(Platforms.continue_sales(client,jobs,job,['A'],'selected',after_process_exit=True))
        client.request.assert_called_once_with('collection.retry_selected',command_id='original',indices=[0],sequence=2,retry_id='selected')

    def test_chunk_copy_and_garbage_collection_keep_run(self):
        import normal_orders,storage_maintenance,data_maintenance
        normal_orders.Repository(self.work)
        r=self.repo.claim(self.repo.manual(self.s)['id']);data={'rows':[{'sku':str(i),'qty':i} for i in range(2000)]}
        self.repo.save_value(r['id'],'large',data,r['epoch'])
        with self.repo.db(True) as db:data_maintenance.collect_shared(db,self.root)
        self.assertEqual(self.repo.value(r['id'],'large'),data)
        target=self.root/'backup.sqlite3';storage_maintenance.backup(self.work.path,target)
        self.assertEqual(sr.Repository(target).value(r['id'],'large'),data)
    def test_ready_result_late_epoch_and_delete_guards(self):
        r=self.repo.claim(self.repo.manual(self.s)['id']);self.repo.request_pause(r['id'])
        self.repo.save_value(r['id'],'checkpoint',{'x':1},r['epoch'])
        with self.assertRaises(wr.Conflict):self.repo.delete_run(r['id'],self.repo.get(r['id'])['revision'])
        self.repo.recover();new=self.repo.get(r['id']);self.repo.request_pause(r['id'],True);ended=self.repo.get(r['id'])
        self.repo.delete_run(r['id'],ended['revision']);self.assertEqual(self.repo.list()['total'],0)
    def test_one_process_owner(self):
        a=sr.SchedulerLock(self.root);b=sr.SchedulerLock(self.root)
        self.assertTrue(a.acquire());self.assertFalse(b.acquire());a.close();self.assertTrue(b.acquire());b.close()
    def test_pages_and_once_expiry(self):
        for i in range(55):self.repo.manual(self.s)
        p=self.repo.list();self.assertEqual((len(p['items']),p['next_offset'],p['total']),(50,50,55));self.assertEqual(len(self.repo.list(offset=50)['items']),5)
        with self.assertRaises(ValueError):self.repo.list(limit=101)
        self.assertIsNone(sr.next_at({**self.s,'kind':'once','date':'2026-11-03'},self.now))


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.work,self.s,self.basis,self.catalogs,self.manifest=fixture(self.root)
        self.repo=sr.Repository(self.work.path,lambda:datetime(2026,11,4,6,30,tzinfo=sr.TZ))
        self.backend=LocalBackend(self.catalogs,self.manifest)
    def tearDown(self):self.tmp.cleanup()
    def run_one(self):
        r=self.repo.manual(self.s);se.Runner(self.repo,self.backend,self.root).execute(r['id']);return self.repo.get(r['id'])
    def test_actual_spawn_core_equivalence_and_restore(self):
        r=self.run_one();self.assertEqual(r['state'],'COMPLETE',r['message'])
        record,basis=se.workbench_record(self.repo,r['id']);self.assertEqual(list(record['payload']['results']),['A','B']);self.assertEqual(wr.summary(record['payload'])['completed'],2)
        loaded=record['payload']['loaded']
        from priority_list import catalog_profile
        expected_profile=catalog_profile(self.catalogs)
        self.assertEqual(record['payload']['profile'],expected_profile)
        self.assertEqual([r['_must_sell'] for r in record['payload']['results']['A']['rows']],[True,False])
        self.assertEqual([r['_must_sell'] for r in record['payload']['results']['B']['rows']],[False,False])
        manual=batch_service.run(loaded,basis['jobs'],expected_profile,1)
        for s,result in record['payload']['results'].items():
            expected=wr.loads(wr.dumps(manual[s]))
            # Exact full trees, except documented process/timing diagnostics.
            result.pop('task');expected.pop('task');result.pop('created');expected.pop('created');self.assertEqual(result,expected)
        self.assertEqual(self.work.list_jobs(),[])
    def test_verified_legacy_arrangement_uses_new_saved_defaults_on_schedule(self):
        now = datetime(2026, 11, 4, 6, 29, tzinfo=sr.TZ)
        self.repo.clock = lambda: now
        self.repo.save_schedule({**self.s, 'enabled': True})
        record = self.work.stores()['A']
        record['preset']['config']['warehouses']['河北仓'] = [10, 2]
        self.work.save_stores({'A': record})
        now += timedelta(minutes=1)
        run, = self.repo.due()
        se.Runner(self.repo, self.backend, self.root).execute(run['id'])
        current = self.repo.get(run['id'])
        self.assertEqual(current['state'], 'COMPLETE', current['message'])
        self.assertEqual(self.repo.value(run['id'], 'basis')['jobs']['A']['warehouses']['河北仓'], [10, 2])
        self.assertEqual(self.backend.calls, ['acquire', 'sales', 'catalogs', 'release'])
        self.assertEqual(self.repo.due(), [])

    def test_continue_legacy_attempt_stopped_before_any_platform_work(self):
        created = self.repo.manual(self.s)
        claimed = self.repo.claim(created['id'])
        self.repo.change(created['id'], epoch=claimed['epoch'], state='REVIEW',
                         message='预案、默认参数或准入依据已变化，请重新检查运行条件后新建任务')
        record = self.work.stores()['A']
        record['preset']['config']['warehouses']['河北仓'] = [10, 2]
        self.work.save_stores({'A': record})

        reopened = sr.Repository(self.work.path, self.repo.clock)
        original = reopened.get(created['id'])
        self.assertEqual(original['external'], {})
        self.assertIsNone(reopened.value(created['id'], 'basis'))
        reopened.resume(created['id'], original['revision'])
        se.Runner(reopened, self.backend, self.root).execute(created['id'])
        current = reopened.get(created['id'])
        self.assertEqual(current['state'], 'COMPLETE', current['message'])
        self.assertEqual(current['trigger_key'], created['trigger_key'])
        self.assertEqual(reopened.list()['total'], 1)
        self.assertEqual(self.backend.calls, ['acquire', 'sales', 'catalogs', 'release'])

    def test_changed_plan_scope_requires_saving_the_arrangement_again(self):
        plans_repo = plans.Repository(self.work.path)
        old = plans_repo.list()[0]
        plans_repo.save({**old, 'stores': old['stores'][:1]}, old)
        run = self.run_one()
        self.assertEqual(run['state'], 'REVIEW')
        self.assertIn('门店范围已变化', run['message'])
        self.assertEqual(self.backend.calls, ['release'])

    def test_arrangement_without_verification_still_stops_before_platform_work(self):
        self.s.pop('check_signature')
        run = self.run_one()
        self.assertEqual(run['state'], 'REVIEW')
        self.assertEqual(self.backend.calls, ['release'])
    def test_warehouse_failure_blocks_dependents_and_success_not_recalculated(self):
        self.backend.data={'山东仓':self.catalogs['山东仓']}
        r=self.run_one();self.assertEqual(r['state'],'REVIEW');self.assertEqual([s['state'] for s in r['stores']],['FAILED','READY'])
        old=self.repo.value(r['id'],'result:B');self.repo.resume(r['id'],r['revision'],['A']);self.backend.data=self.catalogs
        se.Runner(self.repo,self.backend,self.root).execute(r['id']);self.assertEqual(self.repo.get(r['id'])['state'],'COMPLETE')
        self.assertEqual(self.repo.value(r['id'],'result:B'),old)
    def test_ended_partial_run_retries_only_selected_and_persists_scope(self):
        both=self.backend.manifest
        self.backend.manifest={**both,'files':both['files'][:1]}
        first=self.run_one()
        self.assertEqual([s['state'] for s in first['stores']],['READY','FAILED'])
        saved=self.repo.value(first['id'],'result:A')
        self.repo.request_pause(first['id'],True)
        ended=self.repo.get(first['id'])
        with self.assertRaises(wr.Conflict):self.repo.resume(ended['id'],ended['revision'])
        with self.assertRaises(ValueError):self.repo.resume(ended['id'],ended['revision'],['A','B'])
        queued=self.repo.resume(ended['id'],ended['revision'],['B'])
        self.assertEqual(queued,self.repo.resume(ended['id'],ended['revision'],['B']))
        self.repo.claim(ended['id']);self.repo.recover()
        paused=self.repo.get(ended['id'])
        resumed=self.repo.resume(paused['id'],paused['revision'])
        self.assertEqual(resumed['retry_id'],queued['retry_id'])
        self.assertEqual(resumed['retry_codes'],['B'])
        self.backend.manifest=both
        se.Runner(self.repo,self.backend,self.root).execute(ended['id'])
        self.assertEqual(self.repo.get(ended['id'])['state'],'COMPLETE')
        self.assertEqual(self.repo.value(ended['id'],'result:A'),saved)

    def test_signed_after_queue_blocks_recollection_before_platform_access(self):
        import normal_orders
        self.backend.manifest={**self.backend.manifest,'files':self.backend.manifest['files'][:1]}
        partial=self.run_one()
        self.repo.resume(partial['id'],partial['revision'],['B'])
        normal_orders.Repository(self.work)
        with self.repo.db(True) as db:
            db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',
                       (partial['day'],'B','regular','isolated-order','BH-B',1))
        self.backend.calls.clear()
        se.Runner(self.repo,self.backend,self.root).execute(partial['id'])
        self.assertEqual(self.backend.calls,['release'])
        self.assertIn('已有签发：B',self.repo.get(partial['id'])['message'])

    def test_pause_recovers_frozen_profile_and_no_daily_overwrite(self):
        stop=threading.Event();self.backend.cancel=stop;r=self.repo.manual(self.s)
        se.Runner(self.repo,self.backend,self.root,stop).execute(r['id']);r=self.repo.get(r['id']);self.assertEqual(r['state'],'PAUSED')
        original=self.repo.value(r['id'],'basis');self.repo.resume(r['id'],r['revision'])
        d=self.work.stores()['A'];d['preset']['config']['warehouses']['河北仓']=[20,2];self.work.save_stores({'A':d})
        self.backend.cancel=None;se.Runner(self.repo,self.backend,self.root).execute(r['id'])
        self.assertEqual(self.repo.get(r['id'])['state'],'COMPLETE');self.assertEqual(self.repo.value(r['id'],'basis'),original)
    def test_same_day_new_run_does_not_overwrite_earlier_plans_or_draft(self):
        first=self.run_one();saved=self.repo.value(first['id'],'result:A')
        second=self.run_one()
        self.assertNotEqual(first['id'],second['id'])
        self.assertEqual(self.repo.get(first['id']),first)
        self.assertEqual(second['state'],'COMPLETE',second['message'])
        self.assertEqual(self.repo.value(first['id'],'result:A'),saved)
        self.assertEqual(self.work.list_jobs(),[])

    def test_recovery_keeps_browser_wait_bound_to_original_command(self):
        r=self.repo.manual(self.s);running=self.repo.claim(r['id'])
        self.repo.change(r['id'],epoch=running['epoch'],stage='等待浏览器可用',external={'ax':'original-command'})
        self.repo.recover();again=self.repo.next_queued()
        self.assertEqual((again['id'],again['external'],again['state']),(r['id'],{'ax':'original-command'},'QUEUED'))
    def test_non_alphabetical_plan_order_survives_chunks_and_restore(self):
        old=plans.Repository(self.work.path).list()[0];new=copy.deepcopy(old);new['stores'].reverse();plans.Repository(self.work.path).save(new,old)
        self.s['stores'].reverse();self.s['check_signature']=se.check_signature(se.freeze(self.root,self.s,'2026-11-04'))
        self.manifest['files'].reverse();r=self.run_one();self.assertEqual(r['state'],'COMPLETE')
        record,basis=se.workbench_record(self.repo,r['id']);self.assertEqual(list(record['payload']['loaded']['stores']),['B','A']);self.assertEqual(list(record['payload']['results']),['B','A'])

    def test_publication_version_checks_before_any_write(self):
        r=self.run_one();before={w:wc.Repository(self.root).current_digest(w) for w in self.catalogs}
        changed=copy.deepcopy(self.catalogs['河北仓']);changed['source_path']+='-changed';wc.Repository(self.root).save(changed)
        with self.assertRaisesRegex(ValueError,'当前仓资料已变化'):se.publish_catalogs(self.repo,r['id'],self.root)
        self.assertEqual(wc.Repository(self.root).current_digest('山东仓'),before['山东仓'])
        self.assertEqual(self.repo.get(r['id'])['state'],'COMPLETE')

    def test_publication_idempotent_and_results_immutable(self):
        r=self.run_one();before=self.repo.value(r['id'],'result:A')
        se.publish_catalogs(self.repo,r['id'],self.root)
        with patch.object(wc.Repository,'save_locked',side_effect=AssertionError('must not republish')):
            se.publish_catalogs(self.repo,r['id'],self.root)
        self.assertEqual(self.repo.get(r['id'])['publication']['state'],'PUBLISHED')
        self.assertEqual(self.repo.value(r['id'],'result:A'),before)

    def test_continue_original_without_new_warehouse_read(self):
        before=copy.deepcopy(self.catalogs)
        run=self.repo.manual(self.s);rid=run['id']
        older=LocalBackend(before,{'files':self.manifest['files'][:1]})
        se.Runner(self.repo,older,self.root).execute(rid)
        partial=self.repo.get(rid);self.assertEqual(partial['state'],'REVIEW')
        done=self.repo.value(rid,'result:A')
        newer=copy.deepcopy(before)
        newer['山东仓']['items']['0001']['可用数']=60
        newer['山东仓']['revision']=wc.revision(newer['山东仓']['items'])
        wc.Repository(self.root).save(newer['山东仓'])
        self.repo.resume(rid,partial['revision'])
        still_original=LocalBackend(newer,self.manifest)
        se.Runner(self.repo,still_original,self.root).execute(rid)
        self.assertNotIn('catalogs',still_original.calls)
        self.assertEqual(self.repo.get(rid)['state'],'COMPLETE')
        self.assertEqual(self.repo.value(rid,'catalogs'),before)
        self.assertEqual(self.repo.value(rid,'result:A'),done)

    def test_publication_recovers_replace_without_receipt(self):
        run=self.run_one();rid=run['id']
        master=wc.Repository(self.root)
        actual=master.save_locked
        saved=[]
        def replace_then_interrupt(instance,data,**kw):
            actual(data,**kw);saved.append(data['warehouse'])
            raise OSError('synthetic interruption after replace')
        candidates=copy.deepcopy(self.repo.value(rid,'catalogs'))
        for c in candidates.values():c['imported_at']='2026-11-04T10:00:00'
        get_value=self.repo.value
        with patch.object(self.repo,'value',side_effect=lambda run_id,key,*args:
                          candidates if key=='catalogs' else get_value(run_id,key,*args)):
            with patch.object(wc.Repository,'save_locked',replace_then_interrupt):
                with self.assertRaisesRegex(OSError,'after replace'):
                    se.publish_catalogs(self.repo,rid,self.root)
            publication=self.repo.get(rid)['publication'];wh=saved[0]
            self.assertEqual(publication['state'],'PUBLISHING')
            self.assertEqual(master.current_digest(wh),publication['intents'][wh])
            def save_other(instance,data,**kw):
                self.assertNotEqual(data['warehouse'],wh);actual(data,**kw)
            with patch.object(wc.Repository,'save_locked',save_other):se.publish_catalogs(self.repo,rid,self.root)
        self.assertEqual(self.repo.get(rid)['publication']['state'],'PUBLISHED')

    def test_publication_holds_invalid_stock_and_publishes_valid_sibling(self):
        run=self.run_one();rid=run['id']
        catalogs=self.repo.value(rid,'catalogs')
        invalid=catalogs['山东仓']
        for item in invalid['items'].values():item['可用数']=None
        invalid['revision']=wc.revision(invalid['items'])
        repo=wc.Repository(self.root);old=repo.current_digest('山东仓');get_value=self.repo.value
        with patch.object(self.repo,'value',side_effect=lambda run_id,key,*args:
                          catalogs if key=='catalogs' else get_value(run_id,key,*args)):
            message,published=se.publish_catalogs(self.repo,rid,self.root)
        self.assertIn('山东仓',message);self.assertEqual(list(published),['河北仓'])
        self.assertEqual(repo.current_digest('山东仓'),old)
        state=self.repo.get(rid)['publication']
        self.assertEqual(state['state'],'NEEDS_REVIEW')
        self.assertIn('可用数全部缺失',state['errors']['山东仓'])

if __name__=='__main__':unittest.main()
