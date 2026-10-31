import copy
import sys
import tempfile
import threading
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import openpyxl
import stocking_lists as sl
import work_repository as wr
from runtime_support import Cancelled


class StockingTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def workbook(self,name='list.xlsx',rows=None):
        p=self.root/name;b=openpyxl.Workbook();w=b.active;w.title='清单'
        for row in rows or [['目标','名称','编码'],[12,'甲','0001'],[24,'乙','12345678901234567890']]:w.append(row)
        b.save(p);b.close();return p
    def spec(self,path):
        info=sl.inspect([path])[0];return {**info,'sheet':'清单','mapping':{'sku':'编码','name':'名称','target':'目标'}}
    def test_reordered_headers_and_long_exact_codes(self):
        data=sl.import_list([self.spec(self.workbook())],'新品','new')
        self.assertEqual([i['sku'] for i in data['items']],['0001','12345678901234567890'])
        self.assertEqual(data['items'][1]['target'],24)
    def test_only_code_minimum_template(self):
        p=self.workbook(rows=[['商品编码'],['0002']]);info=sl.inspect([p])[0]
        d=sl.import_list([{**info,'sheet':'清单','mapping':{'sku':'商品编码'}}],'清单','plan')
        self.assertEqual(d['items'][0]['target'],None)
    def test_duplicate_same_keeps_source_rows(self):
        p=self.workbook();a=self.spec(p);d=sl.import_list([a,a],'重复','new')
        self.assertEqual(len(d['items']),2);self.assertEqual(len(d['items'][0]['source_rows']),2)
    def test_conflict_not_last_wins(self):
        a=self.spec(self.workbook());b=self.spec(self.workbook('b.xlsx',[['目标','名称','编码'],[48,'甲','0001']]))
        with self.assertRaisesRegex(ValueError,'冲突'):sl.import_list([a,b],'冲突','new')
    def test_numeric_long_code_rejected(self):
        p=self.workbook(rows=[['目标','名称','编码'],[12,'不可靠',1234567890123456789]])
        with self.assertRaisesRegex(ValueError,'15位'):sl.import_list([self.spec(p)],'坏','new')
    def test_duplicate_header_rejected(self):
        p=self.workbook(rows=[['目标','编码','编码'],[12,'a','b']]);info=sl.inspect([p])[0]
        with self.assertRaises(ValueError):sl.import_list([{**info,'sheet':'清单','mapping':{'sku':'编码','target':'目标'}}],'重复头','new')
    def test_change_after_mapping_rejected(self):
        p=self.workbook();spec=self.spec(p);p.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'变化'):sl.import_list([spec],'变更','new')
    def test_versions_attachments_and_optimistic_lock(self):
        d=sl.import_list([self.spec(self.workbook())],'原名','new');db=wr.Repository(self.root/'settings');repo=sl.Repository(db.path)
        a=repo.save(d);self.assertEqual(a['version'],1)
        self.workbook().unlink();b=copy.deepcopy(a);b['name']='新名';b['revision']=sl.revision(b)
        b=repo.save(b,expected=a['revision']);self.assertEqual(b['version'],2)
        self.assertEqual(len(repo.all()),2)
        with self.assertRaises(wr.Conflict):repo.save(a,expected=a['revision'])
        self.assertEqual(repo.all()[1]['name'],'原名')
    def test_cancel_does_not_save(self):
        d=sl.import_list([self.spec(self.workbook())],'原名','new');db=wr.Repository(self.root/'settings');repo=sl.Repository(db.path)
        stop=threading.Event();stop.set()
        with self.assertRaises(Cancelled):repo.save(d,cancel=stop)
        self.assertEqual(repo.all(),[])
    def test_invalid_period(self):
        d=sl.import_list([self.spec(self.workbook())],'原名','new');d.update(valid_from='2026-11-01',valid_to='2026-10-01');d['revision']=sl.revision(d)
        with self.assertRaises(ValueError):sl.validate(d)
    def test_uncached_formula_is_not_blank_default(self):
        path=self.workbook(rows=[['目标','名称','编码'],['=12+12','甲','0001']])
        with self.assertRaisesRegex(ValueError,'公式无缓存'):sl.import_list([self.spec(path)],'公式','plan')
    def test_per_item_targets_need_no_fallback(self):
        d=sl.import_list([self.spec(self.workbook())],'逐行目标','plan',{'strategy':'target','target':None})
        self.assertEqual(d['name'],'逐行目标');sl.validate(d)
        d['items'][0]['target']=None;d['revision']=sl.revision(d)
        with self.assertRaisesRegex(ValueError,'部分商品没有目标'):sl.validate(d)


if __name__=='__main__':unittest.main()
