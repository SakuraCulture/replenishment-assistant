import copy,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import openpyxl
from openpyxl.packaging.custom import StringProperty
import order_identity as identity,normal_exports as ne,lifecycle_io as io,lifecycle_batch as batch,order_lifecycle as lc,work_repository as wr
import test_lifecycle as fixture

class Identity(unittest.TestCase):
    def setUp(self):self.f=fixture.LifecycleTests();self.f.setUp();self.path=self.f.root/'signed.xlsx'
    def tearDown(self):self.f.tearDown()
    def export(self):
        order=self.f.repo.get(wr.today(),'T');ne.export(order,self.path,warehouse='河北仓');return self.path
    def read(self):
        result=batch.read([self.path],'return');self.assertEqual(result['issues'],[]);return result['documents']
    def test_hidden_metadata_business_columns_and_automatic_binding(self):
        self.export();b=openpyxl.load_workbook(self.path)
        self.assertEqual(b.sheetnames,[*ne.SHEETS,identity.SHEET]);self.assertEqual(b[identity.SHEET].sheet_state,'veryHidden');self.assertTrue(b.security.lockStructure)
        for name in ne.SHEETS:self.assertEqual([c.value for c in b[name][1]],ne.HEADERS+['决策理由']);self.assertFalse(b[name].column_dimensions['U'].hidden)
        self.assertEqual(identity.read(b)[0]['number'],self.f.h['number']);b.close()
        docs=self.read();p=batch.prepare(self.f.path,docs);self.assertEqual(len(p['plans']),1);self.assertEqual(p['plans'][0]['head']['number'],self.f.h['number'])
    def test_quantity_changes_sort_and_added_rows_do_not_destroy_identity(self):
        self.export();b=openpyxl.load_workbook(self.path);s=b[ne.SHEETS[-1]];s['Q2']=0
        values=[None]*21;values[0]='测试店';values[1]='T';values[4]='EXTRA';values[16]=2;values[18]='河北仓';s.append(values)
        old=[c.value for c in s[2]];s.delete_rows(2);s.append(old);b.save(self.path);b.close()
        d=self.read()[0];self.assertEqual([r['sku'] for r in d['scan']['rows']],['EXTRA','0001'])
        p=batch.prepare(self.f.path,[d]);self.assertEqual(p['plans'][0]['payload']['extra'],['EXTRA'])
    def test_old_hidden_reference_rejects_new_revision(self):
        self.export();self.f.second_revision()
        with self.assertRaisesRegex(wr.Conflict,'原签发已变化'):batch.prepare(self.f.path,self.read())
    def test_hidden_removed_can_bind_but_conflicting_properties_fail(self):
        self.export();b=openpyxl.load_workbook(self.path);del b[identity.SHEET]
        for prop in list(b.custom_doc_props.names):del b.custom_doc_props[prop]
        b.save(self.path);b.close();docs=self.read();self.assertFalse(docs[0]['scan']['rows'][0]['signed_number'])
        docs[0]['target']=self.f.h;self.assertEqual(len(batch.prepare(self.f.path,docs)['plans']),1)
        self.path.unlink();self.export();b=openpyxl.load_workbook(self.path);b.custom_doc_props['补货单号'].value='BAD';b.save(self.path);b.close()
        with self.assertRaisesRegex(ValueError,'冲突'):io.inspect([self.path])
    def test_ambiguous_same_store_warehouse_requires_record_selection(self):
        self.export();b=openpyxl.load_workbook(self.path);entries=identity.read(b)
        other=[{**e,'number':'BH-OTHER','day':'2026-11-01'} for e in entries]
        for prop in list(b.custom_doc_props.names):del b.custom_doc_props[prop]
        identity.write(b,entries+other);b.save(self.path);b.close();docs=self.read();r=docs[0]['scan']['rows'][0]
        self.assertFalse(r['signed_number']);self.assertEqual(len(r['identity_candidates']),2)
        with self.assertRaisesRegex(ValueError,'缺号'):batch.prepare(self.f.path,docs)
        split=batch.split_document(docs[0],[0],self.f.h);self.assertEqual(len(batch.prepare(self.f.path,split)['plans']),1)
        with self.assertRaises(ValueError):batch.split_document(docs[0],[0],{**self.f.h,'number':'NOT-IN-METADATA'})
    def test_row_store_conflict_does_not_fallback_to_manual(self):
        self.export();b=openpyxl.load_workbook(self.path);b[ne.SHEETS[-1]]['B2']='OTHER';b.save(self.path);b.close()
        result=batch.read([self.path],'return');self.assertFalse(result['documents']);self.assertIn('冲突',result['issues'][0]['message'])
    def test_metadata_corruption_and_formulas_rejected(self):
        self.export();b=openpyxl.load_workbook(self.path);b[identity.SHEET]['D2']='=BAD';b.save(self.path);b.close()
        with self.assertRaisesRegex(ValueError,'校验失败'):io.inspect([self.path])
    def test_return_export_preserves_metadata_and_no_number_column(self):
        import lifecycle_exports as ex
        self.f.save();folder=self.f.root/'returns';folder.mkdir();result=ex.export_returns(self.f.path,[self.f.view()],folder,'combined',lambda _:None,None)
        self.assertIsNone(result['error']);self.path=Path(result['files'][0]);docs=self.read();self.assertEqual(docs[0]['scan']['rows'][0]['base_quantity'],'5')
    def test_legacy_unpartitioned_export_feedback_still_readable(self):
        import merchant_feedback
        order=self.f.repo.get(wr.today(),'T');ne.export(order,self.path)
        scan=merchant_feedback.scan([self.path]);plan=merchant_feedback.prepare(self.f.path,scan,{})
        self.assertTrue(plan)
    def test_minimum_template_notes_not_treated_as_unknown_business_sheet(self):
        io.template('return',self.path);b=openpyxl.load_workbook(self.path);b.worksheets[0].append(['0001',3,None,None]);b.save(self.path);b.close()
        docs=self.read();docs[0]['target']=self.f.h
        self.assertEqual(batch.prepare(self.f.path,docs)['plans'][0]['payload']['rows'][0]['base_quantity'],'3')

if __name__=='__main__':unittest.main()
