"""Whole-result warehouse reports and the actual export controls; synthetic only."""
import copy,os,tempfile,threading,unittest
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import openpyxl
import warehouse_stockout as service
import warehouse_stockout_exports as exports
from warehouse_stockout_worker import execute
from warehouse_stockout_views import defaults
from stockout_common import file_hash,today
from test_warehouse_stockout import workbook,ROWS


class ExportFixture(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.repo=service.Repository(self.root);self.repo.initialize();self.options={'start':today(),'end':today()};self.cancel=threading.Event();self.calls=[]
    def rpc(self,*a,**kw):self.calls.append(a);raise AssertionError('No platform calls')
    def ingest(self,rows=ROWS,warehouse='山东仓'):
        source=workbook(self.root/('input-'+warehouse+'.xlsx'),rows)
        return execute(self.repo,'ingest',{'path':str(source),'warehouse':warehouse},self.cancel,lambda _:None,self.rpc)
    def read(self,path):
        b=openpyxl.load_workbook(path,data_only=True);self.addCleanup(b.close);return b


class ExportTests(ExportFixture):
    def test_all_pages_filters_identities_and_no_business_writes(self):
        rows=[[f'{n:08d}','匹配商品'+str(n),0 if n%2==0 else 5,'启用','','','饮料','000S1','供货一'] for n in range(601)]
        self.ingest(rows+[['other','不匹配',0,'启用','','','食品','S2','供货二']]);self.ingest(rows[:1],warehouse='河北仓')
        marker=self.root/'workbench.sqlite3';marker.write_bytes(b'not-a-business-database-open-it-nowhere')
        options={**self.options,'search':'匹配商品','filters':{'warehouse':['山东仓'],'supplier':['供货一'],'category':['饮料']}}
        before=file_hash(self.repo.path);page=service.detail(self.repo,options);self.assertEqual(len(page['rows']),200);self.assertTrue(page['more'])
        target=self.root/'whole.xlsx';exports.export(self.repo,options,target,defaults()['combinations']);book=self.read(target)
        self.assertEqual(list(book['范围汇总'].values)[1][1:],(601,301,301/601,0,0))
        detail=list(book['商品明细1'].values);self.assertEqual(len(detail),602);self.assertEqual({r[1] for r in detail[1:]},{'山东仓'})
        self.assertEqual(detail[1][2],'00000000');self.assertEqual(book['商品明细1']['C2'].data_type,'s')
        self.assertEqual(len(list(book['大仓商品1'].values)),602)
        headings=next(book['供应商分类1'].values);self.assertIn('供应商编码',headings);self.assertIn('供应商名称',headings)
        self.assertEqual(book['范围汇总']['D2'].number_format,'0.00%');self.assertIsNotNone(book['商品明细1'].auto_filter.ref)
        self.assertEqual(file_hash(self.repo.path),before);self.assertEqual(marker.read_bytes(),b'not-a-business-database-open-it-nowhere');self.assertFalse(self.calls)
    def test_detail_only_modes_do_not_change_summary_rate(self):
        self.ingest()
        for mode,count in [('all',8),('included',5),('excluded',3),('pending',0)]:
            with self.subTest(mode=mode):
                path=self.root/(mode+'.xlsx');exports.export(self.repo,self.options,path,[],detail_mode=mode)
                book=self.read(path);self.assertEqual(book['范围汇总']['D2'].value,.4)
                sheet=next(s for s in book if s.title.startswith('商品明细'));self.assertEqual(len(list(sheet.values))-1,count)
                self.assertNotIn('大仓总览1',book.sheetnames)
                self.assertEqual(dict(book['口径与覆盖'].values)['商品明细范围'],exports.DETAIL_MODES[mode])
        with self.assertRaisesRegex(ValueError,'至少'):exports.export(self.repo,self.options,self.root/'nothing.xlsx',[],include_detail=False)
        self.assertFalse((self.root/'nothing.xlsx').exists())
    def test_search_escapes_literal_wildcards_and_sql_like_text(self):
        code='p%_!'+chr(92)
        self.ingest([[code,'含特殊字符',0,'启用','','','饮料','S','供货'],['plain','普通商品',1,'启用','','','饮料','S','供货']])
        for term in ('%','_','!',chr(92)):
            with self.subTest(term=term):
                opt={**self.options,'search':term};self.assertEqual(service.query(self.repo,opt)['base'],1)
                self.assertEqual(service.detail(self.repo,opt)['rows'][0]['sku'],code)
        self.assertEqual(service.query(self.repo,{**self.options,'search':"' OR 1=1 --"})['base'],0)
    def test_pending_and_confirmed_zero_keep_different_rates(self):
        for name,stock,expected in [('unknown',None,None),('available',3,0)]:
            self.ingest([['0001',name,stock,'启用','','','饮料','S','供货']]);path=self.root/(name+'.xlsx')
            exports.export(self.repo,self.options,path,defaults()['combinations']);book=self.read(path)
            self.assertEqual(book['范围汇总']['D2'].value,expected)
            if expected==0:self.assertEqual(book['范围汇总']['D2'].data_type,'n')
    def test_cancel_save_failure_and_existing_file_never_publish_partial(self):
        self.ingest();target=self.root/'report.xlsx';target.write_bytes(b'keep')
        with self.assertRaises(ValueError):exports.export(self.repo,self.options,target,defaults()['combinations'])
        self.assertEqual(target.read_bytes(),b'keep')
        self.cancel.set()
        with self.assertRaises(Exception):exports.export(self.repo,self.options,self.root/'cancelled.xlsx',defaults()['combinations'],cancel=self.cancel)
        self.cancel.clear()
        def progress(message):
            if '正在保存' in message:self.cancel.set()
        with self.assertRaises(Exception):exports.export(self.repo,self.options,self.root/'save-cancel.xlsx',defaults()['combinations'],cancel=self.cancel,progress=progress)
        self.cancel.clear()
        with patch.object(openpyxl.Workbook,'save',side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError):exports.export(self.repo,self.options,self.root/'save-fail.xlsx',defaults()['combinations'])
        self.assertFalse((self.root/'cancelled.xlsx').exists());self.assertFalse((self.root/'save-cancel.xlsx').exists());self.assertFalse((self.root/'save-fail.xlsx').exists());self.assertFalse(list(self.root.glob('.warehouse-*.xlsx')))
    def test_rollover_and_long_colliding_titles_lose_no_rows(self):
        self.ingest();combos=[{'name':'同'*39+'甲','dimensions':['warehouse']},{'name':'同'*39+'乙','dimensions':['warehouse']}]
        target=self.root/'rollover.xlsx'
        with patch.object(exports,'ROWS_PER_SHEET',2):exports.export(self.repo,self.options,target,combos)
        book=self.read(target);self.assertTrue(all(len(n)<=31 for n in book.sheetnames));self.assertEqual(len(set(n.casefold() for n in book.sheetnames)),len(book.sheetnames))
        details=[s for s in book if s.title.startswith('商品明细')];self.assertEqual(len(details),4);self.assertEqual(sorted(r[2] for s in details for r in list(s.values)[1:]),sorted(r[0] for r in ROWS))
    def test_worker_forwards_detail_scope_without_platform_calls(self):
        self.ingest();path=self.root/'worker.xlsx'
        result=execute(self.repo,'export',{'options':self.options,'destination':str(path),'combinations':[],'detail_mode':'excluded'},self.cancel,lambda _:None,self.rpc)
        self.assertEqual(result,str(path));book=self.read(path);self.assertEqual(len(list(book['商品明细1'].values)),4);self.assertFalse(self.calls)


class ExportUiTests(ExportFixture):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def page(self):
        from PySide6.QtWidgets import QWidget
        from warehouse_stockout_ui import Page
        self.ingest();host=QWidget();host.repository=SimpleNamespace(folder=self.root);owner=self
        class Entry:
            worker=None;background=False
            def start(self,op,args,callback=None,**kw):
                owner.calls.append((op,copy.deepcopy(args)))
                value=execute(owner.repo,op,args,owner.cancel,lambda _:None,owner.rpc)
                if callback:callback(value)
            def pause(self):pass
        entry=Entry();page=Page(host,entry);page.refresh_metadata();self.addCleanup(lambda:(page.deleteLater(),host.deleteLater(),self.app.processEvents()));return page,entry
    def test_dialog_defaults_presets_and_preferences_unchanged(self):
        from warehouse_stockout_ui import ExportDialog,checked
        from PySide6.QtWidgets import QMessageBox
        from PySide6.QtCore import Qt
        before=defaults();config=copy.deepcopy(before);d=ExportDialog(None,config,['day','warehouse'],self.options)
        selected=checked(d.listing);self.assertEqual(len(selected),1);self.assertEqual(selected[0]['dimensions'],['day','warehouse'])
        keys=[d.listing.item(n).data(Qt.ItemDataRole.UserRole)['dimensions'] for n in range(d.listing.count())]
        self.assertIn(['warehouse','supplier'],keys);self.assertIn(['warehouse','category'],keys);self.assertEqual(config,before)
        d.check_all(False);d.details.setChecked(False)
        with patch.object(QMessageBox,'information') as message:d.accept();self.assertTrue(message.called)
        self.assertEqual(d.result(),0);d.details.setChecked(True);d.accept();self.assertEqual(d.combinations,[])
        d.deleteLater();self.app.processEvents()
    def test_actual_button_keeps_detail_scope_and_global_export_directory(self):
        from warehouse_stockout_ui import ExportDialog
        from PySide6.QtWidgets import QFileDialog,QMessageBox
        import export_settings
        page,entry=self.page();page.tabs.setCurrentIndex(2);page.mode.setCurrentIndex(page.mode.findData('excluded'))
        folder=self.root/'configured-export';folder.mkdir();export_settings.save(self.root/export_settings.FILENAME,str(folder));path=folder/'native.xlsx';proposed=[]
        def choose(*args,**kw):proposed.append(args[2]);return str(path),'Excel (*.xlsx)'
        def accept(dialog):dialog.accept();return dialog.result()
        with patch.object(ExportDialog,'exec',accept),patch.object(QFileDialog,'getSaveFileName',side_effect=choose),patch.object(QMessageBox,'information') as message:
            page.export_btn.click();self.assertTrue(message.called)
        self.assertEqual(Path(proposed[0]).parent,folder);request=next(args for op,args in reversed(self.calls) if op=='export')
        self.assertEqual(request['detail_mode'],'excluded');self.assertEqual(request['combinations'],[]);self.assertNotIn('mode',request['options'])
        book=self.read(path);self.assertEqual(len(list(book['商品明细1'].values)),4);self.assertEqual(book['范围汇总']['D2'].value,.4)
    def test_queries_cannot_silently_cancel_an_active_export(self):
        page,entry=self.page();entry.worker=SimpleNamespace(operation='export');before=len(self.calls);page.controls();page.query();page.view();page.refresh_metadata()
        self.assertEqual(len(self.calls),before);self.assertFalse(page.export_btn.isEnabled());self.assertFalse(page.tabs.isEnabled());self.assertTrue(page.pause.isEnabled())
        entry.worker=None;page.controls();self.assertTrue(page.export_btn.isEnabled());self.assertTrue(page.tabs.isEnabled())


if __name__=='__main__':unittest.main()
