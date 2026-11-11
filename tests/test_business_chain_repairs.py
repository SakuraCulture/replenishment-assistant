"""Unknown values and complete parameter files through real application entry points."""
import copy,os,tempfile,unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
import openpyxl
import order_analytics as analytics
from funnel_compact_export import metrics
import parameter_tables as tables
import parameter_snapshot,store_parameter_import as spi,store_parameter_ui as ui
import work_repository as wr
from test_store_parameter_ui import Host
import test_order_analytics as seed


class UnknownMetrics(unittest.TestCase):
    def setUp(self):
        f=seed.AnalyticsTests();f.setUp();self.addCleanup(f.tearDown)
        self.report=f.report();self.rows=copy.deepcopy(self.report['rows'])
    def test_absent_stage_is_unknown_in_both_projections(self):
        for row in self.rows:row['valid']['receipt']=False;row['quantities']['receipt']=None
        front=analytics.aggregate(self.rows)['stages']['receipt'];excel=metrics(self.rows,'receipt')
        self.assertIsNone(front['value']);self.assertIsNone(front['skus']);self.assertEqual(excel,(None,None,None))
    def test_all_positive_unpriced_is_unknown_money_but_known_skus(self):
        for row in self.rows:row['price']=None;row['valid']['signed']=True;row['quantities']['signed']=Decimal(3)
        front=analytics.aggregate(self.rows)['stages']['signed'];excel=metrics(self.rows,'signed')
        self.assertIsNone(front['value']);self.assertEqual(front['value'],excel[2]);self.assertEqual(front['skus'],excel[0]);self.assertGreater(front['skus'],0)
    def test_confirmed_zero_remains_zero_even_without_price(self):
        for row in self.rows:row['price']=None;row['valid']['signed']=True;row['quantities']['signed']=Decimal(0)
        front=analytics.aggregate(self.rows)['stages']['signed']
        self.assertEqual((front['skus'],front['value']),(0,Decimal(0)));self.assertEqual(metrics(self.rows,'signed'),(0,Decimal(0),Decimal(0)))
    def test_mixed_price_partial_value_and_shared_cohort_ratios_preserved(self):
        self.rows[0]['price']=None
        for stage in analytics.STAGES:
            front=analytics.aggregate(self.rows)['stages'][stage];excel=metrics(self.rows,stage)
            self.assertEqual((front['skus'],front['value']),(excel[0],excel[2]))
    def test_actual_funnel_cells_show_unknown_for_missing_stage_and_all_unpriced(self):
        from PySide6.QtWidgets import QApplication,QTableWidget,QComboBox,QLabel
        from types import SimpleNamespace
        from order_analytics_ui import AnalyticsWidget
        app=QApplication.instance() or QApplication([])
        for row in self.rows:row['valid']['receipt']=False;row['quantities']['receipt']=None;row['price']=None
        self.report['rows']=self.rows;self.report['groups']={g:analytics.aggregate([r for r in self.rows if r['group']==g]) for g in analytics.GROUPS}
        choice=QComboBox();choice.addItem('SKU','skus');choice.addItem('货值','value')
        shown=SimpleNamespace(report=self.report,metric_choice=choice,funnel=QTableWidget(0,6),coverage_note=QLabel())
        AnalyticsWidget.render_funnel(shown)
        self.assertEqual(shown.funnel.item(0,5).text(),'未确认')
        choice.setCurrentIndex(1);AnalyticsWidget.render_funnel(shown)
        self.assertIn('未确认 元',shown.funnel.item(0,2).text());self.assertNotIn('None',shown.funnel.item(0,5).text())
        shown.funnel.deleteLater();shown.coverage_note.deleteLater();choice.deleteLater();app.processEvents()


class CompleteParameters(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name)
        self.host=Host(self.root/'settings');self.record=spi.base();self.record['name']='A'
        self.record['ui'].update(primary='山东仓',secondary='河北仓',mode=2,amount='1234')
        c=self.record['preset']['config'];c['parameters']['minimum_reference_units']=3;c['parameters']['probability']=.95
        c['warehouses']['山东仓']=[6,3];c['exception']=False
        c['profile_parameters']={'快销':{'probability':.85}}
        self.host.work_repository.save_stores({'A':self.record})
    def export(self):
        path=self.root/'all.xlsx';before=self.host.work_repository.path.read_bytes()
        parameter_snapshot.export(self.host.work_repository,path)
        self.assertEqual(self.host.work_repository.path.read_bytes(),before)
        return path
    def test_native_full_export_roundtrip_all_editable_values_and_scopes(self):
        path=self.export();data=spi.read([path]);self.assertTrue(data['complete']);self.assertTrue(data['notices'])
        unchanged=spi.plan(data,{'A':self.record});self.assertFalse(unchanged['errors']);self.assertFalse(unchanged['changed'])
        for scope in ('defaults','current','both'):
            with self.subTest(scope=scope):
                host=Host(self.root/scope);snapshot=ui.capture(host,data);prepared=ui.preview(data,snapshot,scope)
                self.assertEqual(prepared['errors'],[]);ui.commit(host,data,snapshot,scope,prepared)
                if scope!='current':self.assertEqual(wr.Repository(self.root/scope).stores()['A']['preset']['config']['parameters']['minimum_reference_units'],3)
                if scope!='defaults':
                    self.assertEqual(host.config_for('A')['parameters']['probability'],.95);self.assertEqual(host.uis['A']['primary'],'山东仓')
                else:self.assertEqual(host.config_for('A')['parameters']['probability'],spi.base()['preset']['config']['parameters']['probability'])
    def test_complete_template_contains_all_six_editable_tables(self):
        from import_templates import write
        path=self.root/'template.xlsx';write('complete_parameters',path);book=openpyxl.load_workbook(path)
        try:
            self.assertTrue(set(tables.COMPLETE_EDITABLE)<=set(book.sheetnames));self.assertIn(tables.MAPPING_REFERENCE,book.sheetnames)
            self.assertEqual(book[tables.MARKER]['B1'].value,'complete_parameters')
            self.assertTrue(book[tables.MAPPING_REFERENCE].protection.sheet)
        finally:book.close()
    def test_old_full_v3_export_mapping_is_explicit_readonly_reference(self):
        path=self.export();book=openpyxl.load_workbook(path)
        book[tables.MAPPING_REFERENCE].title='分类映射';book['分类映射'].append(['A','A','修改也不覆盖全局','日百'])
        book[tables.MARKER]['B1']=None;book.save(path);book.close()
        data=spi.read([path]);self.assertTrue(data['notices'])
        self.assertFalse(any(f[0]=='category_profiles' for r in data['rows'] for f in r['values']))
        self.assertEqual(spi.plan(data,{'A':self.record})['errors'],[])
    def test_partial_v3_mapping_still_requires_global_entry(self):
        path=self.root/'partial.xlsx';book=openpyxl.Workbook();ws=book.active;ws.title='分类映射';ws.append(tables.SHEETS['分类映射']);ws.append(['A','A','食品','日百']);book.create_sheet(tables.MARKER).append([tables.FORMAT_SCOPED]);book.save(path);book.close()
        with self.assertRaisesRegex(ValueError,'全局'):spi.read([path])
    def test_custom_profile_full_import_and_invalid_value_no_partial_commit(self):
        self.record['preset']['config']['weight_profiles']['自定义']={'alpha':.2,'w1':.6,'w2':.4}
        self.host.work_repository.save_stores({'A':self.record});path=self.export();data=spi.read([path])
        result=spi.plan(data,{'A':spi.base()});self.assertEqual(result['errors'],[])
        self.assertEqual(result['values']['A']['preset']['config']['weight_profiles']['自定义'],{'alpha':.2,'w1':.6,'w2':.4})
        book=openpyxl.load_workbook(path);sheet=book['门店通用参数'];col=[c.value for c in sheet[1]].index('库存目标分位概率')+1;sheet.cell(2,col,0);book.save(path);book.close()
        invalid=spi.read([path]);snapshot=ui.capture(self.host,invalid);prepared=ui.preview(invalid,snapshot,'both');before=self.host.work_repository.path.read_bytes()
        self.assertTrue(prepared['errors'])
        with self.assertRaises(ValueError):ui.commit(self.host,invalid,snapshot,'both',prepared)
        self.assertEqual(self.host.work_repository.path.read_bytes(),before)
    def test_unchanged_preview_explains_no_effect_and_readonly_reference(self):
        data=spi.read([self.export()]);snapshot=ui.capture(self.host,data)
        dialog=ui.Preview(None,self.host,data,snapshot,'defaults')
        self.assertFalse(dialog.go.isEnabled());self.assertIn('没有参数变化',dialog.error.text());self.assertIn('不写回',dialog.note.text())
        dialog.reject();dialog.deleteLater();self.app.processEvents()
    def test_full_ui_buttons_reach_the_complete_template_and_reader(self):
        from workbench import Workbench
        from parameter_scene import ParameterScene
        from PySide6.QtWidgets import QPushButton
        with patch.dict(os.environ,REPLENISHMENT_DATA_DIR=str(self.root/'button-settings')):
            host=Workbench()
            try:
                with patch.object(ui,'import_parameters') as read,patch('import_ui.download') as download:
                    scene=ParameterScene(host)
                    next(b for b in scene.findChildren(QPushButton) if b.text()=='导入完整门店参数').click()
                    next(b for b in scene.findChildren(QPushButton) if b.text()=='下载完整门店参数模板').click()
                    self.assertEqual(read.call_args.kwargs['kind'],'complete_parameters');self.assertEqual(download.call_args.args[-1],'complete_parameters')
                    scene.deleteLater();self.app.processEvents()
            finally:host._discard_close=True;host.close();host.deleteLater();self.app.processEvents()
