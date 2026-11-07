import copy
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
import openpyxl
from test_stockout_analysis import AnalysisTests
import stockout_ai as service
import stockout_metrics as metrics
from stockout_facts import load_store


class StockoutAI(unittest.TestCase):
    setUp=AnalysisTests.setUp
    sales=AnalysisTests.sales
    publish=AnalysisTests.publish
    catalog=AnalysisTests.catalog

    def query(self):return {'start':'2026-11-01','end':'2026-11-07'}

    def day(self,day,rows):
        run=self.repo.new_run(day=day)
        load_store(self.repo,run['id'],'S',self.sales(rows),observed=day+'T08:00:00+08:00')
        self.repo.publish(run['id']);return run

    def test_lazy_context_never_creates_database(self):
        root=self.root/'absent'
        self.assertFalse(service.context(root)['available']);self.assertFalse(root.exists())
        command="import automation_gateway,sys;assert 'stockout_ai' not in sys.modules;assert 'stockout_repository' not in sys.modules"
        subprocess.run([sys.executable,'-c',command],check=True)

    def test_native_equivalence_negative_unknown_and_exclusions(self):
        catalog=self.catalog([['A','启用',3,'饮品','甲',''],['B','禁用',4,'食品','乙',''],['C','启用',4,'食品','甲','淘便考核']])
        self.publish([('A',2,-3),('B',2,0),('C',1,1),('D',None,0)],{'山东仓':catalog})
        query={**self.query(),'view':'both'};report=service.analyze(self.root,query,['supplier','category','store'])
        self.assertEqual(report['overview'],metrics.query(self.repo,service.options(query)))
        self.assertEqual(report['first_cut'],metrics.cuts(self.repo,service.options(query),['supplier','category','store'],limit=100))
        self.assertEqual(report['overview']['total']['zero'],1)
        evidence=service.read(self.root,report,'evidence',store='S',sku='A')
        self.assertEqual(evidence['items'][0]['stock'],0)
        self.assertEqual(evidence['items'][0]['stock_raw'],'-3')

    def test_new_publication_does_not_move_old_analysis_or_export(self):
        self.publish([('A',2,0),('B',3,1)])
        report=service.analyze(self.root,self.query());old=copy.deepcopy(report['overview'])
        self.publish([('A',2,1),('B',3,1)])
        self.assertEqual(service.read(self.root,report,'zero')['total'],1)
        self.assertEqual(metrics.query(service.PinnedRepository(self.root,report['basis']),report['basis']['query']),old)
        self.assertEqual(service.analyze(self.root,self.query())['overview']['total']['zero'],0)
        result=service.export(self.root,report,self.root/'pinned.xlsx')
        self.assertEqual(result['publications'],report['basis']['publications'])
        from stockout_exports import export
        export(service.PinnedRepository(self.root,report['basis']),report['basis']['query'],self.root/'native.xlsx',dimensions=report['dimensions'])
        def cells(path):
            book=openpyxl.load_workbook(path,read_only=True,data_only=True)
            try:return {ws.title:list(ws.values) for ws in book if ws.title!='口径与覆盖'}
            finally:book.close()
        self.assertEqual(cells(self.root/'native.xlsx'),cells(self.root/'pinned.xlsx'))

    def test_expired_or_removed_snapshot_cannot_silently_requery(self):
        self.publish([('A',2,0)]);report=service.analyze(self.root,self.query())
        with patch('stockout_ai.time.time',return_value=report['basis']['expires_at']+1):
            with self.assertRaisesRegex(ValueError,'过期'):service.read(self.root,report,'zero')
        with self.repo.connect(write=True) as db:db.execute('UPDATE run_stores SET error=?',('changed',))
        with self.assertRaisesRegex(ValueError,'变化'):service.read(self.root,report,'zero')

    def test_compare_uses_same_store_sku_and_missing_days_break_streak(self):
        self.day('2026-11-01',[('A',1,0),('B',1,0)])
        self.day('2026-11-02',[('A',1,0),('B',1,1)])
        self.day('2026-11-04',[('A',1,0),('B',1,1),('C',1,0)])
        report=service.analyze(self.root,self.query());value=report['comparison']
        self.assertEqual((value['comparable_base'],value['entered_comparable_scope'],value['missing_one_observation']),(2,1,1))
        self.assertEqual(value['interval_days'],2)
        rows=service.read(self.root,report,'repeated')['items'];a=next(r for r in rows if r['sku']=='A')
        self.assertEqual((a['zero_observations'],a['consecutive_days']),(3,2))
        self.assertEqual([x['day'] for x in report['trend']],['2026-11-01','2026-11-02','2026-11-04'])

    def test_evidence_and_filters_cannot_expand_scope(self):
        self.publish([('A',2,0)]);report=service.analyze(self.root,{**self.query(),'filters':{'store':['S'],'sku':['A']}})
        with self.assertRaisesRegex(ValueError,'不在'):service.read(self.root,report,'evidence',store='X',sku='A')
        with self.assertRaisesRegex(ValueError,'不在'):service.read(self.root,report,'evidence',store='S',sku='B')
        with service.PinnedRepository(self.root,report['basis']).connect() as db:
            with self.assertRaises(Exception):db.execute('DELETE FROM publications')


class StockoutGateway(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_automation_gateway import GatewayRegression
        GatewayRegression.setUpClass();cls.app=GatewayRegression.app
    def setUp(self):
        from test_automation_gateway import GatewayRegression
        GatewayRegression.setUp(self)
    def tearDown(self):
        from test_automation_gateway import GatewayRegression
        GatewayRegression.tearDown(self)
    def wait(self):
        from test_automation_gateway import GatewayRegression
        GatewayRegression.wait(self)
    def test_async_page_export_idempotency_disconnect_and_reconnect(self):
        from stockout_repository import Repository
        from test_stockout_analysis import workbook
        repo=Repository(self.win.repository.folder);repo.initialize()
        repo.save_settings({'stores':[{'code':'S','name':'测试门店'}],'routes':{'S':{'primary':'山东仓','secondary':None}},'profile':None})
        run=repo.new_run(day='2026-11-07')
        path=workbook(Path(self.tmp.name)/'input.xlsx',['仓库/门店编码','商品编码','商品名称','月销','可用库存'],[['S','A','商品A',3,0]])
        load_store(repo,run['id'],'S',path,observed='2026-11-07T08:00:00+08:00');repo.publish(run['id'])
        args={'context_token':self.g.token(),'operation_key':'one','query':{'start':'2026-11-07','end':'2026-11-07'}}
        task=self.g.dispatch('query_stockout_analysis',args);self.wait();tid=task['task_id']
        self.assertEqual(self.g.task_record(tid)['state'],'completed')
        self.assertEqual(self.g.dispatch('query_stockout_analysis',args)['task_id'],tid)
        summary=self.g.dispatch('stockout_analysis_results',{'task_id':tid})
        pageargs={'task_id':tid,'level':'zero'};pending=self.g.dispatch('stockout_analysis_results',pageargs)
        self.assertTrue(pending['page_pending']);self.wait()
        self.assertEqual(self.g.dispatch('stockout_analysis_results',pageargs)['items'][0]['sku'],'A')
        exportargs={'task_id':tid,'snapshot_id':summary['snapshot_id'],'context_token':self.g.token(),'operation_key':'export','directory':self.tmp.name}
        e=self.g.dispatch('export_stockout_analysis',exportargs);self.wait()
        self.assertEqual(self.g.task_record(e['task_id'])['state'],'completed')
        self.assertEqual(self.g.dispatch('export_stockout_analysis',exportargs)['task_id'],e['task_id'])
        self.g.disable();self.g.enable()
        from automation_protocol import CommandError
        with self.assertRaises(CommandError):self.g.dispatch('stockout_analysis_results',{'task_id':tid})
