"""Number-only public import; private revision/audit invariants stay intact."""
import copy,hashlib,unittest
import openpyxl
from openpyxl.packaging.custom import StringProperty
import test_lifecycle as fixture
import lifecycle_io as io,order_lifecycle as lc,work_repository as wr

class SimpleImportTests(unittest.TestCase):
    def setUp(self):self.f=fixture.LifecycleTests();self.f.setUp();self.n=0
    def tearDown(self):self.f.tearDown()
    def scan(self,rows,headers=None,stage='return',props=None):
        self.n+=1;p=self.f.root/f'simple-{self.n}.xlsx';b=openpyxl.Workbook();s=b.active
        headers=headers or ['补货单号（必填）','商品编码（必填）','门店确认数量（必填）'];s.append(headers)
        for row in rows:s.append(row)
        for k,v in (props or {}).items():b.custom_doc_props.append(StringProperty(name=k,value=v))
        b.save(p);b.close();f=io.inspect([p])[0];t=f['sheets'][0]
        return io.scan(stage,[{**f,**t,'mapping':io.auto_mapping(stage,t['headers']),'defaults':{'数量单位':'库存单位'},'order_number_only':True}])
    def test_minimum_template_explicit_labels_and_no_internal_columns(self):
        p=self.f.root/'template.xlsx';io.template('return',p);b=openpyxl.load_workbook(p)
        self.assertEqual(list(next(b.active.values)),['补货单号（必填）','商品编码（必填）','门店确认数量（必填）','商品名称（选填）','备注（选填）'])
        self.assertIn('空白',b.active['C1'].comment.text);b.close()
    def test_minimal_number_code_quantity_no_upstream_retyping(self):
        f=self.f;before=f.old_tables();scan=self.scan([[f.h['number'],'0001',6]])
        plan=lc.prepare_numbered(f.path,scan);r=plan['plans'][0]['payload']['rows'][0]
        self.assertEqual(r['upstream_quantity'],str(f.h['products']['0001']['quantity']));self.assertEqual(r['base_quantity'],'6')
        lc.commit(f.path,plan);self.assertEqual(f.view()['rows'][0][3],6);self.assertEqual(f.old_tables(),before)
    def test_blank_still_zero_but_missing_quantity_column_rejected(self):
        f=self.f;scan=self.scan([[f.h['number'],'0001',None]])
        r=lc.prepare_numbered(f.path,scan)['plans'][0]['payload']['rows'][0]
        self.assertEqual((r['base_quantity'],r['quantity_origin']),('0','blank'))
        with self.assertRaisesRegex(ValueError,'缺少字段映射'):self.scan([[f.h['number'],'0001']],['补货单号','商品编码'])
    def test_source_revision_is_not_user_identity_current_order_is_unique(self):
        f=self.f;f.save();old=copy.deepcopy(lc.history(f.path,f.h,'return'));f.second_revision()
        scan=self.scan([[f.h['number'],'0001',8,'V001']],['补货单号','商品编码','补货数量','签发修订'],props={'补货单号':f.h['number'],'修订':'V001'})
        self.assertIsNone(scan['rows'][0]['revision']);plan=lc.prepare_numbered(f.path,scan)
        self.assertEqual(plan['plans'][0]['head']['revision'],2);lc.commit(f.path,plan)
        self.assertEqual(lc.history(f.path,f.h,'return'),old)
        self.assertEqual(len(lc.current_targets(f.path)),2)
    def test_current_order_changed_after_preview_rejected(self):
        f=self.f;p=lc.prepare_numbered(f.path,self.scan([[f.h['number'],'0001',3]]));f.second_revision()
        with self.assertRaises(wr.Conflict):lc.commit(f.path,p)
        self.assertEqual(lc.history(f.path,f.h,'return'),[])
    def test_batch_numbers_separate_warehouses_and_preserve_order(self):
        f=self.f;sd=f.heads[1];scan=self.scan([[sd['number'],'0002',2],[f.h['number'],'0001',3]])
        p=lc.prepare_numbered(f.path,scan);self.assertEqual([x['head']['number'] for x in p['plans']],[sd['number'],f.h['number']]);lc.commit(f.path,p)
        self.assertEqual(f.view()['rows'][0][3],3);self.assertEqual(f.view(sd)['rows'][0][3],2)
    def test_single_context_fills_number_global_missing_or_wrong_rejected(self):
        f=self.f;scan=self.scan([[None,'0001',4]])
        with self.assertRaisesRegex(ValueError,'缺少补货单号'):lc.prepare_numbered(f.path,scan)
        p=lc.prepare_numbered(f.path,scan,f.h);self.assertEqual(p['plans'][0]['head']['number'],f.h['number'])
        with self.assertRaisesRegex(ValueError,'不一致'):lc.prepare_numbered(f.path,self.scan([[f.heads[1]['number'],'0002',2]]),f.h)
        with self.assertRaises(ValueError):lc.prepare_numbered(f.path,self.scan([['UNKNOWN','0001',4]]))
    def test_order_number_conflicts_with_source_warehouse_rejected(self):
        f=self.f;scan=self.scan([[f.h['number'],'0001',4,'山东仓']],['补货单号','商品编码','门店确认数量','供货仓'])
        with self.assertRaisesRegex(ValueError,'不一致'):lc.prepare_numbered(f.path,scan)
    def test_source_claimed_upstream_quantity_is_ignored(self):
        f=self.f;f.save(items=[{'门店确认数量':7}]);headers=['补货单号','商品编码','付款单商品数量','付款单号','实际金额','上环节数量']
        scan=self.scan([[f.h['number'],'0001',3,'P001',9,99999]],headers,'payment')
        r=lc.prepare_numbered(f.path,scan)['plans'][0]['payload']['rows'][0]
        self.assertEqual(r['upstream_quantity'],'7');self.assertEqual(r['difference'],'-4')

if __name__=='__main__':unittest.main()
