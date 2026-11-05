import sys,unittest
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).parent)]
import test_automation_gateway as gateway_fixture
import test_order_analytics as data_fixture
from automation_protocol import CommandError,public_tools
import order_analytics_exports as exports
import openpyxl


class AnalyticsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):gateway_fixture.GatewayRegression.setUpClass()
    def setUp(self):
        self.f=gateway_fixture.GatewayRegression();self.f.setUp();self.g=self.f.g
        self.data=data_fixture.AnalyticsTests();self.data.setUp()
        self.original_work=self.g.host.work_repository;self.g.host.work_repository=self.data.f.work
    def tearDown(self):
        if self.g.worker:self.f.wait()
        self.g.host.work_repository=self.original_work;self.f.tearDown();self.data.tearDown()
    def query(self,key='q'):
        args={'context_token':self.g.token(),'operation_key':key,'query':self.data.spec}
        t=self.g.dispatch('query_order_funnel',args);self.f.wait()
        self.assertEqual(self.g.journal.get(t['task_id'])['state'],'completed')
        return t,args
    def test_async_paging_and_idempotent_query(self):
        before=self.data.path.read_bytes();t,args=self.query()
        self.assertEqual(self.g.dispatch('query_order_funnel',args)['task_id'],t['task_id'])
        page=self.g.dispatch('order_funnel_results',{'task_id':t['task_id'],'level':'detail','limit':1})
        self.assertEqual(page['page']['next_offset'],1);self.assertEqual(page['page']['items'][0]['sku'],'0001')
        last=self.g.dispatch('order_funnel_results',{'task_id':t['task_id'],'level':'detail','offset':1,'limit':1})
        self.assertIsNone(last['page']['next_offset']);self.assertEqual(self.data.path.read_bytes(),before)
    def test_ai_and_manual_workbooks_equal(self):
        t,_=self.query();r=self.g.analytics_reports[t['task_id']]
        args={'context_token':self.g.token(),'operation_key':'ex','task_id':t['task_id'],'snapshot_id':r['snapshot_id'],'directory':str(self.data.root)}
        ex=self.g.dispatch('export_order_funnel',args);self.f.wait();receipt=self.g.journal.get(ex['task_id'])['receipt']
        manual=self.data.root/'manual.xlsx';exports.export(self.data.path,r,manual)
        books=[openpyxl.load_workbook(p) for p in (receipt['path'],manual)]
        try:
            self.assertEqual(books[0].sheetnames,books[1].sheetnames)
            for name in books[0].sheetnames:
                aa=list(books[0][name]);bb=list(books[1][name]);self.assertEqual(len(aa),len(bb))
                for x,y in zip(aa,bb):
                    if name=='统计口径' and x[0].value=='生成时间':continue
                    self.assertEqual([(c.value,c.data_type,c.number_format,c.style_id) for c in x],[(c.value,c.data_type,c.number_format,c.style_id) for c in y])
        finally:
            for book in books:book.close()
        self.assertEqual(self.g.dispatch('export_order_funnel',args)['task_id'],ex['task_id'])
    def test_stale_no_page_or_export_and_permissions(self):
        t,_=self.query();self.data.f.save('return')
        with self.assertRaises(CommandError) as e:self.g.dispatch('order_funnel_results',{'task_id':t['task_id']})
        self.assertEqual(e.exception.code,'STALE_CONTEXT')
        with self.assertRaises(CommandError):self.g.dispatch('query_order_funnel',{'context_token':'old','operation_key':'other','query':self.data.spec})
        annotations={t['name']:t['annotations'] for t in public_tools()}
        self.assertTrue(annotations['order_funnel_results']['readOnlyHint']);self.assertFalse(annotations['export_order_funnel']['destructiveHint'])

if __name__=='__main__':unittest.main()
