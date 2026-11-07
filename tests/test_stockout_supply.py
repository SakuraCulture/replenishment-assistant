"""Saved UI routes, explicit adoption and unchanged business/analysis history."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from stockout_common import atomic_json,file_hash
from stockout_repository import Repository
from stockout_settings import supply_snapshot


class SupplyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.repo=Repository(self.root);self.repo.initialize()
        self.stores=[{'code':s,'name':'测试门店'+s} for s in ('S','T','M')]

    def saved(self,values):
        with closing(sqlite3.connect(self.root/'workbench.sqlite3')) as db,db:
            db.execute('CREATE TABLE IF NOT EXISTS stores(code TEXT PRIMARY KEY,data TEXT NOT NULL)')
            for code,route in values.items():
                db.execute('INSERT OR REPLACE INTO stores VALUES(?,?)',(code,json.dumps({'name':'测试门店'+code,'ui':route})))

    def test_parameter_page_routes_override_stale_cache_and_do_not_resurrect_missing(self):
        self.saved({'S':{'primary':'山东仓','secondary':None},'T':{'primary':'河北仓','secondary':'山东仓'}})
        atomic_json(self.root/'store_supply.json',{s:{'primary':'河北仓','secondary':None} for s in ('S','M')})
        originals={p.name:file_hash(p) for p in self.root.iterdir() if p.is_file()}
        basis=self.repo.adopt_business(self.stores)
        self.assertEqual(basis['routes'],{'S':{'primary':'山东仓','secondary':None},'T':{'primary':'河北仓','secondary':'山东仓'}})
        self.assertEqual(list(basis['route_issues']),['M'])
        self.assertEqual(originals,{p.name:file_hash(p) for p in self.root.iterdir() if p.is_file()})

    def test_no_business_db_is_created_and_no_legacy_route_guessed(self):
        atomic_json(self.root/'store_supply.json',{'S':{'primary':'山东仓','secondary':None}})
        result=self.repo.adopt_business(self.stores)
        self.assertFalse((self.root/'workbench.sqlite3').exists());self.assertEqual(result['routes'],{})
        self.assertEqual(set(result['route_issues']),{'S','T','M'})

    def test_saved_clear_and_invalid_secondary_are_explicit_not_silently_adopted(self):
        self.saved({'S':{'primary':None,'secondary':None},'T':{'primary':'河北仓','secondary':'河北仓'}})
        result=self.repo.adopt_business(self.stores)
        self.assertEqual(result['routes'],{});self.assertIn('无效',result['route_issues']['T'])

    def test_adoption_is_frozen_until_explicit_readoption_and_old_run_keeps_basis(self):
        self.saved({'S':{'primary':'山东仓','secondary':None}})
        first=self.repo.adopt_business(self.stores);run=self.repo.new_run()
        self.saved({'S':{'primary':'河北仓','secondary':'山东仓'}})
        self.assertEqual(self.repo.settings(),first)
        second=self.repo.adopt_business(self.stores)
        self.assertNotEqual(first['version'],second['version'])
        self.assertEqual(self.repo.settings(self.repo.run(run['id'])['basis']),first)

    def test_concurrent_route_change_rejects_adoption_without_changing_analysis_settings(self):
        self.saved({'S':{'primary':'山东仓','secondary':None}})
        first=self.repo.adopt_business(self.stores);count=0
        def changing(folder,stores):
            nonlocal count
            count+=1
            if count==2:self.saved({'S':{'primary':'河北仓','secondary':None}})
            return supply_snapshot(folder,stores)
        with patch('stockout_settings.supply_snapshot',side_effect=changing):
            with self.assertRaisesRegex(ValueError,'采用期间原业务设置变化'):self.repo.adopt_business(self.stores)
        self.assertEqual(self.repo.settings(),first)

    def test_choice_preview_and_adoption_use_the_same_saved_route_source(self):
        from stockout_worker import execute
        self.saved({'S':{'primary':'河北仓','secondary':'山东仓'}})
        def rpc(service,**values):
            self.assertEqual((service,values['action']),('ax','configuration'))
            return {'configured':True,'enabled':True,'targets':[{'buyerWarehouseCode':s['code'],'displayName':s['name']} for s in self.stores]}
        preview=execute(self.repo,'choices',{},None,lambda _:None,rpc)
        adopted=self.repo.adopt_business(preview['stores'])
        self.assertEqual(preview['routes'],adopted['routes']);self.assertEqual(preview['route_issues'],adopted['route_issues'])

    def test_adopted_single_and_dual_routes_select_only_the_assigned_catalog(self):
        from test_stockout_analysis import workbook
        from stockout_facts import load_catalog,load_store
        import stockout_metrics as metrics
        self.saved({'S':{'primary':'山东仓','secondary':None},'T':{'primary':'河北仓','secondary':'山东仓'}})
        self.repo.adopt_business(self.stores[:2]);run=self.repo.new_run()['id'];catalogs={}
        for wh,rows in [('山东仓',[['A','启用',5,'山东类目','山东上游','标杆淘汰']]),
                        ('河北仓',[['A','启用',0,'河北类目','河北上游','淘便考核'],['B','启用',3,'河北类目','河北上游','']])]:
            path=workbook(self.root/(wh+'.xlsx'),['商品编码','商品状态','可用数','分类','供应商名称','虚拟分类'],rows)
            catalogs[wh]=load_catalog(self.repo,path,wh)
        self.repo.change(run,catalogs=catalogs)
        for store in ('S','T'):
            path=workbook(self.root/(store+'.xlsx'),['仓库/门店编码','商品编码','月销','可用库存'],[[store,s,1,0] for s in ('A','B')])
            load_store(self.repo,run,store,path,observed='2026-11-07T08:00:00+08:00')
        self.repo.publish(run)
        rows={(r['store'],r['sku']):r for r in metrics.detail(self.repo,{'start':'2026-11-07'})['items']}
        self.assertEqual((rows['S','A']['warehouse'],rows['T','A']['warehouse'],rows['T','B']['warehouse']),('山东仓','山东仓','河北仓'))
        self.assertEqual(rows['S','B']['source'],'local');self.assertIsNone(rows['S','B']['tags'])
        self.assertEqual(rows['T','A']['category'],'山东类目');self.assertEqual(rows['T','A']['retired'],1)
        self.assertEqual(rows['T','A']['assessment'],0);self.assertIn('山东上游',rows['T','A']['supplier'])


class WorkerCompletionTests(unittest.TestCase):
    def run_with_exit_race(self,messages):
        from types import SimpleNamespace
        from PySide6.QtCore import QObject
        from stockout_task import Task
        class Pipe:
            def __init__(self):self.calls=0;self.messages=list(messages)
            def poll(self,timeout=0):
                self.calls+=1
                return False if self.calls==1 else bool(self.messages)
            def recv(self):return self.messages.pop(0)
            def close(self):pass
        pipe=Pipe()
        process=SimpleNamespace(pid=123,start=lambda:None,is_alive=lambda:False,join=lambda:None,close=lambda:None)
        bridge=SimpleNamespace(yielding=lambda:False,close=lambda:None)
        host=QObject();host.repository=SimpleNamespace(folder=Path('unused-isolated-worker-fixture'))
        task=Task(host,'query',{},bridge_factory=lambda *_:bridge)
        task.ctx=SimpleNamespace(Pipe=lambda:(pipe,SimpleNamespace(close=lambda:None)),Process=lambda **_:process)
        results=[];errors=[];task.succeeded.connect(results.append);task.failed.connect(errors.append)
        task.run()
        return results,errors,pipe

    def test_completed_worker_result_is_drained_after_poll_exit_race(self):
        results,errors,pipe=self.run_with_exit_race([('diagnostic',{'pid':123}),('success',{'base':9,'zero':6})])
        self.assertEqual(errors,[]);self.assertEqual(results,[{'base':9,'zero':6}]);self.assertEqual(pipe.messages,[])

    def test_exit_without_result_or_with_error_still_fails(self):
        for messages in ([],[('error',{'type':'ValueError','message':'隔离查询拒绝','traceback':'isolated failure fixture'})]):
            with self.subTest(messages=messages):
                results,errors,_=self.run_with_exit_race(messages)
                self.assertEqual(results,[]);self.assertEqual(len(errors),1)


if __name__=='__main__':unittest.main()
