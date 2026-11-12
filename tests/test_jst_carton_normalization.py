"""Business examples through the actual batch/commit/export entry, isolated DBs."""
from decimal import Decimal
import copy,unittest
from pathlib import Path
import openpyxl
import test_jst as fixture
import jst_batches as b,jst_orders as j,jst_exports as ex

class Cartons(unittest.TestCase):
    def test_short_stock_allocates_partial_carton_after_batch_reservation(self):
        f=fixture.Service();f.setUp()
        try:
            f.f.save('return',[{'门店确认数量':1090}]);second=f.second_store()
            plan=b.preview_new(f.path,[f.h,second],{'河北仓':fixture.catalog(stock=1094,step=15)},manual_order=True)
            rows=[p['rows'][0] for p in plan['payload']['plans']]
            self.assertEqual([Decimal(r['quantity']) for r in rows],[1080,14])
            self.assertEqual(rows[1]['before'],'14');self.assertEqual(rows[1]['after'],'0')
            self.assertTrue(all(not r['issue'] for r in rows))
            result=b.execute(f.path,plan);self.assertFalse(result['errors']);self.assertEqual(len(result['generated']),2)
            files=ex.export_checked(f.path,result['generated'],f.f.root/'partial-carton')['files']
            quantities=[]
            for file in files:
                book=openpyxl.load_workbook(file['path'],read_only=True,data_only=True)
                try:quantities.append(list(book.active.values)[1][2])
                finally:book.close()
            self.assertEqual(quantities,[1080,14])
        finally:f.tearDown()

    def test_three_business_examples_commit_export_and_retry(self):
        for demand,step,expected in [(1090,15,1080),(800,24,792),(500,12,492)]:
            with self.subTest(demand=demand):
                f=fixture.Service();f.setUp()
                try:
                    f.f.save('return',[{'门店确认数量':demand}]);before=copy.deepcopy(f.f.view()['documents']['return'])
                    plan=b.preview_new(f.path,[f.h],{'河北仓':fixture.catalog(stock=5000,step=step)})
                    self.assertEqual(plan['payload']['plans'][0]['summary']['issues'],0)
                    result=b.execute(f.path,plan);self.assertFalse(result['errors']);self.assertFalse(result['zero'])
                    self.assertEqual(b.execute(f.path,plan)['generated'],result['generated'])
                    p=j.get(f.path,result['generated'][0]);self.assertEqual(Decimal(p['rows'][0]['quantity']),expected)
                    self.assertEqual(f.f.view()['documents']['return'],before)
                    files=ex.export_checked(f.path,result['generated'],f.f.root/'carton-export')['files']
                    book=openpyxl.load_workbook(files[0]['path'],read_only=True,data_only=True)
                    try:self.assertEqual(list(book.active.values)[1][2],expected)
                    finally:book.close()
                    self.assertEqual(Decimal(p['rows'][0]['after']),5000-expected)
                finally:f.tearDown()
