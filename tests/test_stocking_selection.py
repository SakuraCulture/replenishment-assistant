"""Portable picker contracts; optional isolated Qt interaction; no user data."""
import copy,importlib.util,os,sys,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import stocking_selection as ss,stocking_lists as sl,preissue as rules,work_repository as wr
from runtime_support import Cancelled
from test_preissue import result,row,DAY


def catalogs():
    return {wh:{'items':{s:{'商品名称':name,'分类':cat,'商品状态':'禁用' if s=='off' else '启用','_row':i+2} for i,(s,name) in enumerate([('a','汽水' if wh=='河北仓' else '橙味饮料'),('0001','纸巾'),('off','停用商品'),('12345678901234567890','长编码商品')])},'revision':wh+'-v1','source_path':wh+'.xlsx','sheet':'原商品','source_sha256':'qa-'+wh} for wh,cat in [('河北仓','饮品'),('山东仓','日百')]}


def chosen(codes=('a',)):
    return [d for d in ss.products(catalogs(),[row('a')]) if d['sku'] in codes]


def listing(codes=('a',)):
    d=ss.append_products(ss.empty('测试清单','plan'),chosen(codes));return sl.validate(d)


class SelectionTests(unittest.TestCase):
    def test_exact_codes_dedup_sources_and_no_input_mutation(self):
        c=catalogs();before=copy.deepcopy(c);items=ss.products(c,[row('a'),dict(row('a'),B='S2')])
        self.assertEqual([d['sku'] for d in items],['a','0001','off','12345678901234567890']);self.assertEqual(c,before)
        self.assertEqual(len(items[0]['selection_sources']),2);self.assertEqual(items[0]['stores'],['S1','S2'])
        self.assertEqual(items[1]['stores'],[]);self.assertNotIn('K',items[1])
    def test_names_and_correlated_warehouse_category_filter(self):
        d=chosen()[0]
        self.assertTrue(ss.matches(d,'橙味'));self.assertTrue(ss.matches(d,'a 汽水','河北仓','饮品'))
        self.assertFalse(ss.matches(d,'','河北仓','日百'));self.assertFalse(ss.matches(d,'不存在'))
    def test_append_preserves_old_target_order_and_identity(self):
        d=listing();d['items'][0].update(target=48,focused=True);d['revision']=sl.revision(d);old=copy.deepcopy(d)
        n=ss.append_products(d,chosen(('a','0001')))
        self.assertEqual(d,old);self.assertEqual(n['items'][0],d['items'][0]);self.assertEqual([x['sku'] for x in n['items']],['a','0001']);self.assertEqual(n['id'],d['id'])
        self.assertEqual(n['sources'],[]);sl.validate(n)
    def test_cancel_and_invalid_codes_rejected(self):
        stop=threading.Event();stop.set()
        with self.assertRaises(Cancelled):ss.products(catalogs(),[],cancel=stop)
        with self.assertRaises(ValueError):ss.append_products(ss.empty('坏','new'),[{'sku':12345678901234567890}])
        with self.assertRaises(ValueError):ss.append_products(ss.empty('坏','new'),[{'sku':'valid','name':'无依据'}])
    def test_legacy_matched_data_only_and_multiple_warehouses(self):
        rows=[row('a',U='快销'),dict(row('a'),B='S2',AU='山东仓',U='日百'),dict(row('local'),_source='地采商品'),dict(row('bad'),_source='匹配待核')]
        items=ss.products({},rows);self.assertEqual([x['sku'] for x in items],['a']);self.assertEqual(len(items[0]['variants']),2)
    def test_reuse_save_reopen_no_fake_file_attachment(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo=sl.Repository(wr.Repository(tmp).path);d=repo.save(listing());self.assertEqual(d['sources'],[])
            with patch('source_archive.capture',side_effect=AssertionError('must not invent file')):sl.retain(repo.path,d)
            self.assertEqual(repo.all()[0],d)
            other=ss.append_products(d,chosen(('0001',)));self.assertEqual(repo.save(other,d['revision'])['version'],2)
            self.assertEqual(len(repo.all()),2)
    def test_missing_disabled_stopped_and_local_still_pending(self):
        d=listing(('a','0001','off'));r=result([dict(row('a'),_meta={'export_status':'停采','blocking':[]}),dict(row('off'),S='禁用')])
        v=rules.list_edits(r,d,'minimum');self.assertEqual(v['edits'],{});self.assertEqual([x['sku'] for x in v['pending']],['a','0001','off'])
        self.assertIn('缺本店',v['pending'][1]['reason'])
    def test_file_and_selected_list_same_quantities_and_budget_risks(self):
        import openpyxl
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'equivalent.xlsx';b=openpyxl.Workbook();b.active.append(['商品编码','商品名称','目标持有量']);b.active.append(['a','汽水',35]);b.save(p);b.close()
            spec={**sl.inspect([p])[0],'sheet':'Sheet','mapping':{'sku':'商品编码','name':'商品名称','target':'目标持有量'}}
            file=sl.import_list([spec],'相同商品','plan');selected=listing();selected['items'][0]['target']=35;selected['revision']=sl.revision(selected)
            for budget in (None,0,40,100):
                r=result(budget=budget);values=[]
                for listing_value in (file,selected):
                    v=rules.list_edits(r,listing_value,'target',scopes=['zero']);q=rules.preview(r,v['edits'],DAY,lists=v['lists'])
                    values.append(([(x['E'],x.get('_execution_qty'),x.get('_execution_amount'),x['AS'],x['BV'],x.get('_allocated_qty'),x.get('_candidate_qty')) for x in q['result']['rows']],q['errors'],[(x['id'],x['text']) for x in q['risks']]))
                self.assertEqual(*values)
    def test_history_query_source_is_business_readable_and_original_intact(self):
        import history_forms
        d=listing();r=result();v=rules.list_edits(r,d,'target',48);preview=rules.preview(r,v['edits'],DAY,lists=v['lists']);final=rules.finalize(preview,[x['id'] for x in preview['risks']]);before=copy.deepcopy(final)
        order={'payload':{'result':final}}
        rows=history_forms.form(None,order,'签发前调整','a')
        self.assertIn('搜索选商品',str(rows));self.assertIn('河北仓-v1',str(rows));self.assertEqual(final,before)


@unittest.skipUnless(importlib.util.find_spec('PySide6'),'optional Qt runtime')
class PickerUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        from PySide6.QtWidgets import QWidget
        self.tmp=tempfile.TemporaryDirectory();self.host=QWidget();self.host.work_repository=wr.Repository(self.tmp.name)
        self.host.results={'S1':result()};self.host.catalogs=catalogs();self.host.loaded=None;self.host.job_day=DAY;self.host.input_dirty=False;self.dialogs=[]
    def tearDown(self):
        import shiboken6
        for d in reversed(self.dialogs):self.wait(d);d.reject();shiboken6.delete(d)
        shiboken6.delete(self.host);self.tmp.cleanup()
    def wait(self,d):
        self.app.processEvents();end=time.monotonic()+10
        while getattr(d,'task',None) and time.monotonic()<end:self.app.processEvents();time.sleep(.002)
        self.app.processEvents();self.assertIsNone(d.task)
    def picker(self,existing=()):
        from stocking_ui import ProductPicker
        d=ProductPicker(self.host,catalogs(),[row('a')],existing,True);self.dialogs.append(d);self.wait(d);return d
    def library(self):
        from stocking_ui import StockingDialog
        d=StockingDialog(self.host);self.dialogs.append(d);return d
    def test_search_sort_and_hidden_selection(self):
        from PySide6.QtCore import Qt
        d=self.picker();d.search.setText('汽水');self.assertEqual(d.proxy.rowCount(),1);d.check_visible();d.search.setText('纸巾')
        self.assertEqual(list(d.model.checked),['a']);self.assertIn('筛选外1',d.count.text());d.check_visible();d.proxy.sort(1,Qt.SortOrder.DescendingOrder)
        d.finish(False);self.assertEqual([x['sku'] for x in d.selected],['a','0001']);self.assertFalse(d.preview)
    def test_existing_products_cannot_be_selected_twice_and_clear_is_all(self):
        d=self.picker(['a']);d.check_visible();self.assertNotIn('a',d.model.checked);self.assertEqual(len(d.model.checked),3)
        d.search.setText('0001');d.clear_checks();self.assertFalse(d.model.checked);self.assertFalse(d.add_btn.isEnabled());d.reject();self.assertEqual(d.selected,[])
    def test_saved_append_uses_original_revision_for_optimistic_lock(self):
        d=self.library();saved=d.repository.save(listing());d.show_value(saved);extra=ss.append_products(d.value(False),chosen(('0001',)))
        d.show_value(extra,keep_saved=True);d.save(True);self.wait(d);self.assertEqual(d.current['version'],2);self.assertEqual(len(d.current['items']),2)
    def test_blank_name_auto_and_no_preview_without_calculation(self):
        d=self.library();self.assertTrue(d.value(False)['name'].startswith('临时铺货清单'));self.assertEqual(d.value(False)['items'],[])
        self.host.results={};other=self.library();self.assertFalse(other.use_btn.isEnabled())
    def test_remove_only_selected_item_preserves_other_edits(self):
        d=self.library();d.show_value(listing(('a','0001')));d.table.item(0,2).setText('48');d.table.selectRow(1);d.remove_products()
        self.assertEqual([x['sku'] for x in d.current['items']],['a']);self.assertEqual(d.value()['items'][0]['target'],48)
    def test_close_while_indexing_waits_safely_and_discards_result(self):
        from stocking_ui import ProductPicker
        def slow(*args,progress,cancel):
            for _ in range(300):
                if cancel.is_set():raise Cancelled('已取消')
                time.sleep(.002)
            return ss.products(catalogs(),[])
        with patch('stocking_selection.products',slow):
            d=ProductPicker(self.host,catalogs(),[],[],True);self.dialogs.append(d);self.app.processEvents()
            self.assertIsNotNone(d.task);d.reject();self.wait(d);self.assertEqual(d.selected,[]);self.assertIsNone(d.model)


if __name__=='__main__':unittest.main()
