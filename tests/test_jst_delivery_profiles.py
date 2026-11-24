"""Batch edits must preserve unedited stores, warehouses, fields and order evidence."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch
import unittest

import openpyxl

import jst_auto_order_store as store
import jst_delivery_profiles as delivery
import jst_delivery_profiles_xlsx as xlsx
from jst_auto_order_template import CONTACT
from runtime_support import Cancelled
import work_repository as wr


class DeliveryProfiles(unittest.TestCase):
    def setUp(self):
        directory=TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root=Path(directory.name)
        repo=wr.Repository(self.root)
        self.path=repo.path
        with closing(repo.connect()) as db,db:
            db.executemany('INSERT INTO stores VALUES(?,?)',[(code,wr.dumps({'name':name}))
                for code,name in (('00017','合成第一店'),('STORE2','合成第二店'))])
        self.contact={'buyer':'000BUY','recipient':'合成收货人','phone':'00123456789',
                      'province':'河北省','city':'石家庄市','district':'桥西区','address':'原地址'}
        delivery.save_profile(self.path,'00017','河北仓',self.contact,None)
        delivery.save_profile(self.path,'00017','山东仓',{**self.contact,'address':'另一仓地址'},None)

    def data(self):
        return delivery.directory(self.path,{})

    def test_recipient_paste_padding_is_trimmed_and_embedded_lines_are_rejected(self):
        before = self.data()['records']
        key = '00017', '河北仓'
        name = '合成收货人（河北店）'
        saved = delivery.save_profile(self.path, *key, {**self.contact, 'recipient': '\t'+name+'\r\n'}, before[key]['version'])
        self.assertEqual(saved['values'], {**self.contact, 'recipient': name})
        after = self.data()['records']
        self.assertEqual(after['00017','山东仓'], before['00017','山东仓'])
        with self.assertRaisesRegex(ValueError, '收货人姓名.*换行'):
            delivery.save_profile(self.path, *key, {**self.contact, 'recipient': name+'\n另一位'}, saved['version'])
        self.assertEqual(self.data()['records'], after)

    def test_bulk_recipient_uses_the_same_paste_cleanup(self):
        name = '合成批量收货人（河北店）'
        path = self.workbook('clipboard', [(2, '收货人姓名', name+'\r\n')])
        changes = xlsx.plan(path, self.data())
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]['after']['recipient'], name)

    def workbook(self, name, changes=()):
        path=self.root/(name+'.xlsx')
        xlsx.export(path,self.data())
        with closing(openpyxl.load_workbook(path)) as book:
            sheet=book[xlsx.SHEET]
            for row,header,value in changes:
                sheet.cell(row,xlsx.HEADERS.index(header)+1).value=value
            book.save(path)
        return path

    def test_blank_import_template_preserves_text_and_can_update_selected_fields(self):
        path=self.root/'delivery-template.xlsx'
        self.assertEqual(xlsx.download_template(path),str(path))
        self.assertEqual(path.read_bytes(),xlsx.TEMPLATE.read_bytes())
        self.assertEqual(xlsx.plan(path,self.data()),[])
        with self.assertRaisesRegex(ValueError,'新的 .xlsx 文件'):
            xlsx.download_template(path)
        with closing(openpyxl.load_workbook(path)) as book:
            sheet=book[xlsx.SHEET]
            self.assertEqual(tuple(cell.value for cell in sheet[1]),xlsx.HEADERS[:-1])
            self.assertTrue(all(cell.value is None for row in sheet.iter_rows(min_row=2) for cell in row))
            self.assertEqual(sheet['A2'].number_format,'@')
            self.assertEqual(sheet['F2'].number_format,'@')
            warehouse=sheet.data_validations.dataValidation[0]
            self.assertEqual(warehouse.formula1,'"河北仓,山东仓"')
            self.assertIn('C2',warehouse)
            sheet['A2']='00017'
            sheet['C2']='河北仓'
            sheet['F2']='00012345678'
            sheet['J2']='合成模板新地址'
            book.save(path)
        changes=xlsx.plan(path,self.data())
        self.assertEqual(len(changes),1)
        stored=store.save_profiles(self.path,changes)['00017','河北仓']['values']
        self.assertEqual(stored,{**self.contact,'address':'合成模板新地址','phone':'00012345678'})

    def test_round_trip_two_stores_with_separate_warehouses_and_blank_fields(self):
        path=self.workbook('changes',[(2,'买家帐号',None),(2,'地址','第一店河北新地址'),
                                      (5,'地址','第二店山东新地址'),(5,'手机','00000000000')])
        with closing(openpyxl.load_workbook(path)) as book:
            self.assertEqual(book.active['A2'].value,'00017')
            self.assertEqual(book.active['F2'].value,self.contact['phone'])
            self.assertEqual(book.active['F2'].number_format,'@')
        before=self.data()
        changes=xlsx.plan(path,before)
        self.assertEqual([(c['store'],c['warehouse']) for c in changes],
                         [('00017','河北仓'),('STORE2','山东仓')])
        self.assertEqual(self.data(),before)
        self.assertEqual(len(store.save_profiles(self.path,changes)),2)
        records=self.data()['records']
        self.assertEqual(records['00017','河北仓']['values'],{**self.contact,'address':'第一店河北新地址'})
        self.assertEqual(records['00017','山东仓'],before['records']['00017','山东仓'])
        self.assertEqual(records['STORE2','山东仓']['values']['phone'],'00000000000')
        self.assertNotIn(('STORE2','河北仓'),records)
        self.assertEqual(xlsx.plan(path,self.data()),[])
        with closing(wr.read_connection(self.path)) as db:
            self.assertFalse(store.exists(db,'jst_auto_orders'))
            self.assertEqual(db.execute('SELECT count(*) FROM stores').fetchone(),(2,))

    def test_invalid_rows_never_apply_an_earlier_valid_row(self):
        before=self.data()
        for header,value,hint in (('门店编码','OTHER','门店编码'),('供货仓','东北仓','供货仓'),
                                  ('手机',12345678901,'文本'),('地址','=1+1','公式')):
            with self.subTest(header=header,value=value):
                path=self.workbook('invalid-'+header,[(2,'地址','已填新地址'),(5,header,value)])
                with self.assertRaisesRegex(ValueError,'第 5 行.*'+hint):
                    xlsx.plan(path,before)
                self.assertEqual(self.data(),before)
        duplicate=self.workbook('duplicate',[(3,'供货仓','河北仓')])
        with self.assertRaisesRegex(ValueError,'第 3 行.*重复'):
            xlsx.plan(duplicate,before)

    def test_stale_export_and_concurrent_edit_roll_back_whole_batch(self):
        before=self.data()
        path=self.workbook('concurrent',[(2,'地址','河北新地址'),(3,'地址','山东新地址')])
        changes=xlsx.plan(path,before)
        expected=before['records']['00017','山东仓']['version']
        delivery.save_profile(self.path,'00017','山东仓',{**self.contact,'address':'其他窗口的新地址'},expected)
        current=self.data()
        with self.assertRaisesRegex(ValueError,'第 3 行.*重新导出'):
            xlsx.plan(path,current)
        with self.assertRaisesRegex(wr.Conflict,'山东仓.*已更新'):
            store.save_profiles(self.path,changes)
        self.assertEqual(self.data(),current)

    def test_cancel_between_rows_rolls_back_first_write(self):
        before=self.data()
        path=self.workbook('cancel',[(2,'地址','河北新地址'),(3,'地址','山东新地址')])
        cancel=Event()
        put=store._put_profile

        def after_one_write(*args):
            result=put(*args)
            cancel.set()
            return result

        with patch.object(store,'_put_profile',after_one_write):
            with self.assertRaises(Cancelled):
                store.save_profiles(self.path,xlsx.plan(path,before),cancel=cancel)
        self.assertEqual(self.data(),before)
