import sys,unittest
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).parent)]
import test_automation_gateway as gf
import test_inventory_structure as df
from automation_protocol import CommandError,public_tools
import inventory_structure as s
import inventory_structure_exports as ex


class InventoryApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):gf.GatewayRegression.setUpClass()
    def setUp(self):
        self.f=gf.GatewayRegression();self.f.setUp();self.g=self.f.g;self.data=df.ArchiveTests();self.data.setUp()
        self.original=self.g.host.work_repository;self.g.host.work_repository=self.data.f.f.work
    def tearDown(self):
        if self.g.worker:self.f.wait()
        self.g.host.work_repository=self.original;self.f.tearDown();self.data.tearDown()
    def query(self,key='inventory',**spec):
        args={'context_token':self.g.token(),'operation_key':key,'query':{**self.data.spec,**spec}}
        task=self.g.dispatch('query_inventory_structure',args);self.f.wait();self.assertEqual(self.g.journal.get(task['task_id'])['state'],'completed')
        return task,args
    def test_native_equal_paging_and_idempotency(self):
        self.data.old();task,args=self.query(detail_store='T',detail_day=self.data.spec['to'])
        self.assertEqual(self.g.dispatch('query_inventory_structure',args)['task_id'],task['task_id'])
        offset=0;items=[]
        while offset is not None:
            result=self.g.dispatch('inventory_structure_results',{'task_id':task['task_id'],'level':'detail','offset':offset,'limit':1});items.extend(result['page']['items']);offset=result['page']['next_offset']
        report=self.g.inventory_reports[task['task_id']];self.assertEqual(items,s.detail(self.data.path,report,'T',self.data.spec['to']))
        self.assertEqual(result['overview']['total']['value'],90)
    def test_export_equal_and_stale_guard(self):
        import openpyxl
        task,_=self.query();report=self.g.inventory_reports[task['task_id']]
        args={'task_id':task['task_id'],'snapshot_id':report['snapshot_id'],'operation_key':'inventory-export','context_token':self.g.token(),'directory':str(self.data.root),'include_details':True}
        t=self.g.dispatch('export_inventory_structure',args);self.f.wait();state=self.g.journal.get(t['task_id']);self.assertEqual(state['state'],'completed')
        ex.export(self.data.path,report,self.data.root/'manual.xlsx',True)
        books=[openpyxl.load_workbook(p) for p in (state['receipt']['path'],self.data.root/'manual.xlsx')]
        try:
            self.assertEqual(books[0].sheetnames,books[1].sheetnames)
            for name in books[0].sheetnames:
                self.assertEqual([[(c.value,c.data_type,c.number_format,c.style_id) for c in r] for r in books[0][name]],[[(c.value,c.data_type,c.number_format,c.style_id) for c in r] for r in books[1][name]])
        finally:
            for b in books:b.close()
        self.data.f.p['result']['rows'][0]['K']=99;self.data.f.save_payload()
        with self.assertRaises(CommandError):self.g.dispatch('inventory_structure_results',{'task_id':task['task_id']})
    def test_detail_requires_explicit_round_and_readonly_annotation(self):
        task,_=self.query()
        with self.assertRaises(CommandError):self.g.dispatch('inventory_structure_results',{'task_id':task['task_id'],'level':'detail'})
        tools={t['name']:t for t in public_tools()}
        self.assertTrue(tools['query_inventory_structure']['annotations']['readOnlyHint']);self.assertFalse(tools['export_inventory_structure']['annotations']['readOnlyHint'])

if __name__=='__main__':unittest.main()
