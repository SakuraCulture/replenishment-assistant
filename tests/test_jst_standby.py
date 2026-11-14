"""Standby is allocatable; disabled, unknown and admission exclusions remain."""
import unittest
from decimal import Decimal
import test_jst as fixture
import jst_allocation as allocation,jst_batches as batches,jst_orders as orders,warehouse_catalog as wc,jst_exports as exports


class Standby(unittest.TestCase):
    def catalog(self,status='备用',stock=100,step=1,tag=''):
        c=fixture.catalog(stock=stock,step=step);c['items']['0001'].update(商品状态=status,商品标签=tag);c['revision']=wc.revision(c['items']);return c
    def test_status_whitelist_and_exclusions(self):
        for status in ['启用','正常','在用','已启用','备用','禁用','停用','已禁用','停采','其他','',None]:
            with self.subTest(status=status):
                r=allocation.allocate([{'sku':'0001','base_quantity':'40'}],self.catalog(status)['items'],{})[0]
                allowed=status in ('启用','正常','在用','已启用','备用')
                self.assertEqual(r['quantity'],'40' if allowed else '0');self.assertEqual(r['issue'],not allowed)
    def test_standby_keeps_carton_shortage_and_reservation_math(self):
        for stock,balance,expected in [(100,None,'72'),(10,None,'10'),(100,Decimal(14),'14'),(0,None,'0')]:
            with self.subTest(stock=stock,balance=balance):
                r=allocation.allocate([{'sku':'0001','base_quantity':'80'}],self.catalog(stock=stock,step=24)['items'],{} if balance is None else {'0001':balance})[0]
                self.assertFalse(r['issue']);self.assertEqual(r['quantity'],expected);self.assertEqual(r['carton_demand'],'72')
    def test_standby_real_batch_commit_and_xlsx_export(self):
        import openpyxl
        f=fixture.Service();f.setUp()
        try:
            p=batches.preview_new(f.path,[f.h],{'河北仓':self.catalog()});r=batches.execute(f.path,p)
            self.assertFalse(r['errors']);self.assertEqual(len(r['generated']),1)
            self.assertEqual(orders.get(f.path,r['generated'][0])['summary']['quantity'],'40')
            file=exports.export_checked(f.path,r['generated'],f.f.root/'standby-export')['files'][0]['path']
            book=openpyxl.load_workbook(file,read_only=True,data_only=True)
            try:self.assertEqual(list(book.active.values)[1][2],40)
            finally:book.close()
        finally:f.tearDown()
    def test_standby_does_not_bypass_daily_supply_and_other_admission_tags(self):
        for tag in ['日配','二类医疗']:
            with self.subTest(tag=tag):
                f=fixture.Service();f.setUp()
                try:
                    p=batches.preview_new(f.path,[f.h],{'河北仓':self.catalog(tag=tag)})
                    self.assertEqual(p['payload']['plans'][0]['summary']['quantity'],'0')
                    self.assertTrue(p['payload']['plans'][0]['rows'][0]['issue'])
                    r=batches.execute(f.path,p,True);self.assertFalse(r['generated']);self.assertFalse(r['errors'])
                finally:f.tearDown()


if __name__=='__main__':unittest.main()
