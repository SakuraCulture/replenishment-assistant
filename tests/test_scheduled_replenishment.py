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
        w.append(['0001','饮品',2,6,'启用',100,'','合成饮品']);w.append(['0002','饮品',3,1,'启用',100,'','合成单件']);b.save(p);b.close()
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
    def test_chunk_copy_and_garbage_collection_keep_run(self):
        import normal_orders,storage_maintenance,data_maintenance
        normal_orders.Repository(self.work)
        r=self.repo.claim(self.repo.manual(self.s)['id']);data={'rows':[{'sku':str(i),'qty':i} for i in range(2000)]}
        self.repo.save_value(r['id'],'large',data,r['epoch'])
        with self.repo.db(True) as db:data_maintenance.collect_shared(db,self.root)
        self.assertEqual(self.repo.value(r['id'],'large'),data)
        target=self.root/'legacy.sqlite3';storage_maintenance.convert(self.work.path,target,'legacy')
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
        manual=batch_service.run(loaded,basis['jobs'],basis['profile'],1)
        for s,result in record['payload']['results'].items():
            expected=wr.loads(wr.dumps(manual[s]))
            # Exact full trees, except documented process/timing diagnostics.
            result.pop('task');expected.pop('task');result.pop('created');expected.pop('created');self.assertEqual(result,expected)
        self.assertEqual(self.work.list_jobs(),[])
    def test_saved_defaults_change_requires_review(self):
        d=self.work.stores()['A'];d['preset']['config']['warehouses']['河北仓']=[10,2];self.work.save_stores({'A':d})
        r=self.run_one();self.assertEqual(r['state'],'REVIEW');self.assertEqual(self.backend.calls,['release'])
    def test_warehouse_failure_blocks_dependents_and_success_not_recalculated(self):
        self.backend.data={'山东仓':self.catalogs['山东仓']}
        r=self.run_one();self.assertEqual(r['state'],'REVIEW');self.assertEqual([s['state'] for s in r['stores']],['FAILED','READY'])
        old=self.repo.value(r['id'],'result:B');self.repo.resume(r['id'],r['revision'],['A']);self.backend.data=self.catalogs
        se.Runner(self.repo,self.backend,self.root).execute(r['id']);self.assertEqual(self.repo.get(r['id'])['state'],'COMPLETE')
        self.assertEqual(self.repo.value(r['id'],'result:B'),old)
    def test_pause_recovers_frozen_profile_and_no_daily_overwrite(self):
        stop=threading.Event();self.backend.cancel=stop;r=self.repo.manual(self.s)
        se.Runner(self.repo,self.backend,self.root,stop).execute(r['id']);r=self.repo.get(r['id']);self.assertEqual(r['state'],'PAUSED')
        original=self.repo.value(r['id'],'basis');self.repo.resume(r['id'],r['revision'])
        d=self.work.stores()['A'];d['preset']['config']['warehouses']['河北仓']=[20,2];self.work.save_stores({'A':d})
        self.backend.cancel=None;se.Runner(self.repo,self.backend,self.root).execute(r['id'])
        self.assertEqual(self.repo.get(r['id'])['state'],'COMPLETE');self.assertEqual(self.repo.value(r['id'],'basis'),original)
    def test_same_day_overlap_not_overwrite(self):
        first=self.run_one();second=self.run_one();self.assertEqual(first['state'],'COMPLETE');self.assertEqual(second['state'],'REVIEW');self.assertIn('同日',second['message'])

if __name__=='__main__':unittest.main()
