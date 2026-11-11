import copy,hashlib,io,sqlite3,threading,unittest
from contextlib import closing
from decimal import Decimal as D
import openpyxl
import test_order_analytics as seed
from test_funnel_monitor import fact
import order_analytics as a,order_analytics_exports as export,normal_orders as no,source_archive
import funnel_dimension_export as dims
from runtime_support import Cancelled
from xlsx_exports import stream_book

def source(f,wh,headers,rows):
    b=openpyxl.Workbook();s=b.active;s.title='原资料';s.append(headers)
    for row in rows:s.append(row)
    file=f.root/(wh+'-dimension-source.xlsx');b.save(file);b.close();sha=hashlib.sha256(file.read_bytes()).hexdigest();source_archive.capture(f.path,file,sha)
    f.catalogs[wh]={'source_sha256':sha,'sheet':'原资料','revision':sha,'source_path':'not-a-current-catalog.xlsx','items':{}}
    with closing(sqlite3.connect(f.path)) as db,db:db.execute('UPDATE normal_work_revisions SET payload=?',(no.encode({'catalogs':f.catalogs},db),))
    return file,sha

def enrich(f):
    for wh,name,category in [('河北仓','上游甲','饮品'),('山东仓','上游乙','食品')]:
        source(f,wh,['商品编码','商品状态','虚拟分类','供应商名称','分类'],[[sku,'启用','',name,category] for sku in ('0001','0002','EXTRA')])

class HistoricalDimensions(unittest.TestCase):
    def setUp(self):self.f=seed.AnalyticsTests();self.f.setUp();self.addCleanup(self.f.tearDown)
    def test_original_warehouse_attributes_export_without_changing_business_or_metrics(self):
        f=self.f;baseline=f.report()['metrics'];enrich(f);before=f.path.read_bytes();report=f.report()
        self.assertEqual(report['metrics'],baseline);self.assertEqual([(r['warehouse'],r['upstream_supplier'],r['product_category']) for r in report['rows']],[('河北仓','上游甲','饮品'),('山东仓','上游乙','食品')])
        target=f.root/'complete.xlsx';receipt=export.export(f.path,report,target)
        b=openpyxl.load_workbook(target)
        try:
            self.assertIn(dims.NAME,receipt['sheets']);rows=list(b[dims.NAME].values);self.assertEqual(list(rows[0]),dims.HEADERS)
            records=[dict(zip(rows[0],r)) for r in rows[1:]]
            self.assertEqual({r['大仓上游供应商'] for r in records},{'上游甲','上游乙'})
            self.assertEqual({r['商品编码'] for r in records},{'0001','0002'});self.assertTrue(all(b[dims.NAME].cell(i,4).data_type=='s' for i in range(2,4)))
            for stage in a.STAGES:self.assertEqual(sum(D(str(r[a.NAMES[stage]+'商品货值'])) for r in records if r[a.NAMES[stage]+'商品货值'] is not None),report['metrics']['stages'][stage]['value'] or 0)
            self.assertTrue(all(r['签发单号'] and r['聚水潭内部订单号']=='未提供' and r['翱象采购单号']=='未关联' for r in records))
        finally:b.close()
        self.assertEqual(f.path.read_bytes(),before)
    def test_missing_optional_attributes_remain_in_original_funnel(self):
        report=self.f.report();self.assertEqual(report['metrics']['included'],2)
        self.assertTrue(all(r['supplier_state']=='供应商待核' and r['upstream_supplier'] is None for r in report['rows']))
        rows=list(dims.records(report['rows']));self.assertTrue(all(r[5]=='供应商待核' and r[6]=='类目待核' for r in rows))
        self.assertIn('缺少',report['rows'][0]['dimension_notes'][0])
    def test_repeated_supplier_header_and_conflicting_category_do_not_guess_or_change_admission(self):
        f=self.f;source(f,'河北仓',['商品编码','商品状态','虚拟分类','供应商名称','供应商名称','分类'],[['0001','启用','','甲','乙','饮品']])
        f.p['result']['rows'][0]['_category']='食品';f.p['result']['rows'][0]['_meta']['category']='食品';f.save_payload()
        row=f.report()['rows'][0];self.assertEqual(row['scope'],'纳入');self.assertIsNone(row['upstream_supplier']);self.assertIsNone(row['product_category'])
        self.assertTrue(any('表头重复' in v for v in row['dimension_notes']));self.assertTrue(any('来源冲突' in v for v in row['dimension_notes']))
    def test_integrity_failure_is_not_downgraded_to_missing_attributes(self):
        f=self.f;enrich(f);report=f.report();target=f.root/'must-not-publish.xlsx'
        with closing(sqlite3.connect(f.path)) as db,db:db.execute('UPDATE source_files SET content=? WHERE sha=?',(b'corrupted',f.catalogs['河北仓']['source_sha256']))
        with self.assertRaisesRegex(ValueError,'快照校验失败'):export.export(f.path,report,target)
        self.assertFalse(target.exists())
    def test_missing_original_can_use_retained_frozen_fields_without_current_file_fallback(self):
        f=self.f;f.catalogs['河北仓']['items']={'0001':{'商品编码':'0001','商品状态':'启用','虚拟分类':'','供应商名称':'冻结上游','分类':'冻结类目'}}
        with closing(sqlite3.connect(f.path)) as db,db:
            db.execute('UPDATE normal_work_revisions SET payload=?',(no.encode({'catalogs':f.catalogs},db),));db.execute('DELETE FROM source_files')
        row=f.report()['rows'][0];self.assertEqual(row['scope'],'纳入');self.assertEqual((row['upstream_supplier'],row['product_category']),('冻结上游','冻结类目'))
    def test_lifecycle_extra_sku_uses_same_historical_warehouse_and_selected_export_limits_rows(self):
        f=self.f;enrich(f);f.f.save('return',[{'商品编码':'EXTRA','门店确认数量':3}]);report=f.report()
        extra=next(r for r in report['rows'] if r['sku']=='EXTRA');self.assertEqual(extra['upstream_supplier'],'上游甲');self.assertEqual(extra['scope'],'待核')
        target=f.root/'selected.xlsx';export.export(f.path,report,target,orders=[report['heads'][0]['number']]);b=openpyxl.load_workbook(target)
        try:
            rows=list(b[dims.NAME].values);records=[dict(zip(rows[0],r)) for r in rows[1:]]
            self.assertTrue(all(r['供货仓']=='河北仓' for r in records));pending=next(r for r in records if r['商品编码']=='EXTRA')
            self.assertEqual(pending['纳入状态'],'待核');self.assertIsNone(pending['模型建议商品数'])
        finally:b.close()

class DimensionGroups(unittest.TestCase):
    def row(self,**kwargs):return {**fact(**kwargs),'name':'=商品名','category':'快销','upstream_supplier':'=上游甲','product_category':'饮品','supplier_state':'已留存','category_state':'已留存','dimension_notes':[],'dimension_sources':'synthetic frozen source'}
    def test_same_combination_aggregates_period_and_ratios_use_common_stage_facts(self):
        rows=[self.row(),self.row(order='O2',day='2026-10-16',quantities=(20,16,12,None,None),price='3')]
        record=dict(zip(dims.HEADERS,next(dims.records(rows))))
        self.assertEqual(record['涉及签发仓单数'],2);self.assertEqual(record['模型建议商品编码数量'],1);self.assertEqual(record['模型建议商品数'],30);self.assertEqual(record['模型建议商品货值'],80)
        self.assertEqual(record['实际下单/门店确认商品数比值'],D(4)/6)
        self.assertEqual(record['业务开始日期'],'2026-10-15');self.assertIn('实际下单有效1/2条',record['五段资料覆盖'])
        self.assertEqual(record['签发单号'],'O1\nO2')
    def test_warehouses_historical_supplier_changes_units_and_scope_are_not_merged(self):
        row=self.row();rows=[row,{**row,'warehouse':'河北仓'},{**row,'upstream_supplier':'另一上游'},self.row(unit='瓶'),self.row(scope='排除',quantities=(99999,)*5)]
        records=[dict(zip(dims.HEADERS,r)) for r in dims.records(rows)];self.assertEqual(len(records),5)
        self.assertIsNone(next(r for r in records if r['纳入状态']=='排除')['模型建议商品数'])
        buf=io.BytesIO()
        with stream_book() as book:dims.write(book,rows);book.save(buf)
        b=openpyxl.load_workbook(io.BytesIO(buf.getvalue()))
        try:
            ws=b[dims.NAME];self.assertTrue(ws.auto_filter.ref);self.assertEqual(ws.freeze_panes,'F2')
            self.assertTrue(all(c.data_type not in ('f','e') for row in ws for c in row));self.assertEqual(ws['F2'].value,'=上游甲')
        finally:b.close()
        event=threading.Event();event.set()
        with self.assertRaises(Cancelled):list(dims.records(rows,event))
