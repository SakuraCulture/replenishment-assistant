"""Independent warehouse source revisions through actual merchant workbooks."""
import copy,unittest
from contextlib import closing
import normal_delivery as delivery
import merchant_feedback as feedback
import result_semantics as semantics
import warehouse_execution as execution
import test_signing_failures as fixtures


class WarehouseFeedback(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.SigningFailures();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.repo=self.fixture.repo;self.path=self.fixture.work.path;self.payload=self.fixture.payload();self.out=self.fixture.output

    def sign(self,warehouses):
        return delivery.execute_batch(self.repo,delivery.prepare_owned_batch(self.repo,copy.deepcopy(self.payload),['S1'],warehouses={'S1':warehouses}),self.out)

    def heads(self):
        with closing(self.repo.work.connect()) as db:return {h['warehouse']:h for h in execution.heads(db)}

    def initial(self):
        self.sign(['河北仓','山东仓'])
        files=list(self.out.glob('*.xlsx'))
        feedback.commit(self.path,feedback.prepare(self.path,feedback.scan(files),{}))
        return {wh:next(file for file in files if wh in file.name) for wh in ('河北仓','山东仓')}

    def test_same_source_single_warehouse_correction_keeps_other_warehouse_feedback(self):
        import openpyxl
        files=self.initial();heads=self.heads();oid=heads['河北仓']['oid'];revision=heads['河北仓']['revision']
        with closing(self.repo.work.connect()) as db:before=feedback.current(db,oid,revision)
        book=openpyxl.load_workbook(files['河北仓'])
        try:
            changed=0
            for sheet in book:
                headers=[cell.value for cell in sheet[1]]
                if '商品编码' not in headers or '补货数量' not in headers:continue
                sku=headers.index('商品编码')+1;quantity=headers.index('补货数量')+1
                for row in range(2,sheet.max_row+1):
                    if sheet.cell(row,sku).value=='a':sheet.cell(row,quantity,9);changed+=1
            self.assertEqual(changed,1);book.save(files['河北仓'])
        finally:book.close()
        plan=feedback.prepare(self.path,feedback.scan([files['河北仓']]),{})
        self.assertEqual([h['warehouse'] for h in plan['plans'][0]['head']['warehouse_heads']],['河北仓'])
        feedback.commit(self.path,plan)
        with closing(self.repo.work.connect()) as db:after=feedback.current(db,oid,revision)
        self.assertEqual(after['payload']['values']['a'],9)
        self.assertEqual(after['payload']['values']['b'],before['payload']['values']['b'])
        self.assertEqual(after['version'],before['version']+1)
        self.assertEqual(self.heads(),heads)
        sd=feedback.view(self.path,heads['山东仓'])
        self.assertEqual(len(sd['rows']),1);self.assertEqual(sd['rows'][0][4],before['payload']['values']['b'])

    def test_same_store_different_warehouse_source_revisions_can_import_together(self):
        files=self.initial();before=self.heads()
        old_hb=self.out/'hb-source-v1.xlsx';old_hb.write_bytes(files['河北仓'].read_bytes())
        row=next(row for row in self.payload['results']['S1']['rows'] if row['AU']=='河北仓')
        row['AS']+=1
        self.payload['results']['S1']['summary']=semantics.summarize(self.payload['results']['S1']['rows'])
        self.sign(['河北仓']);heads=self.heads()
        self.assertGreater(heads['河北仓']['revision'],heads['山东仓']['revision'])
        self.assertEqual(heads['山东仓'],before['山东仓'])
        plan=feedback.prepare(self.path,feedback.scan([files['河北仓'],files['山东仓']]),{})
        self.assertEqual({(p['head']['revision'],tuple(h['warehouse'] for h in p['head']['warehouse_heads'])) for p in plan['plans']},
                         {(heads['河北仓']['revision'],('河北仓',)),(heads['山东仓']['revision'],('山东仓',))})
        receipts=feedback.commit(self.path,plan)
        self.assertEqual({r['revision'] for r in receipts},{heads['河北仓']['revision'],heads['山东仓']['revision']})
        with closing(self.repo.work.connect()) as db:
            hb=feedback.current(db,heads['河北仓']['oid'],heads['河北仓']['revision'])
            sd=feedback.current(db,heads['山东仓']['oid'],heads['山东仓']['revision'])
        self.assertEqual(hb['payload']['values']['a'],row['AS'])
        self.assertEqual(sd['payload']['values']['b'],self.payload['results']['S1']['rows'][1]['AS'])
        self.assertEqual(self.heads(),heads)
        with self.assertRaisesRegex(ValueError,'不同修订'):feedback.scan([old_hb,files['河北仓']])


if __name__=='__main__':unittest.main()
