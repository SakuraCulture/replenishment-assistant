"""Hand-calculated warehouse rates, isolated XLSX/SQLite, no real platform."""
import copy,json,os,sqlite3,subprocess,sys,tempfile,threading,unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import openpyxl
import warehouse_stockout as service
from warehouse_stockout_worker import execute
from warehouse_stockout_maintenance import preview,remove
from warehouse_stockout_exports import export
from warehouse_stockout_views import defaults
from stockout_common import file_hash,today

HEADERS=['商品编码','商品名称','可用数','商品状态','虚拟分类','商品标签','分类','供应商编码','供应商名称']
ROWS=[['001','零库存',0,'启用','','','饮料','S1','供货一'],['002','负库存',-3,'启用','','','饮料','S1','供货一'],
      ['003','有库存',4,'启用','','','食品','S2','供货二'],['004','淘汰',0,'启用','其他淘汰系列','','食品','S2','供货二'],
      ['005','禁用',0,'禁用','','','食品','S2','供货二'],['006','日配',-1,'启用','','冷链；日配','食品','S2','供货二'],
      ['007','非日配',2,'启用','','非日配','饮料','S1','供货一'],['008','日配专区',3,'启用','','日配专区','饮料','S1','供货一']]

def workbook(path,rows=ROWS,headers=HEADERS):
    b=openpyxl.Workbook();b.active.append(headers)
    for row in rows:b.active.append(row)
    b.save(path);b.close();return path

class WarehouseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.repo=service.Repository(self.root);self.repo.initialize();self.cancel=threading.Event();self.options={'start':today(),'end':today()};self.calls=[]
    def rpc(self,*args,**kw):self.calls.append((args,kw));raise AssertionError('本测试禁止平台调用')
    def ingest(self,rows=ROWS,headers=HEADERS,wh='山东仓'):
        file=workbook(self.root/('source-'+str(len(list(self.root.glob('*.xlsx'))))+'.xlsx'),rows,headers)
        result=execute(self.repo,'ingest',{'path':str(file),'warehouse':wh},self.cancel,lambda _:None,self.rpc);return result['run'],file
    def test_formula_exclusions_and_literal_daily(self):
        self.ingest();r=service.query(self.repo,self.options)
        self.assertEqual((r['total'],r['base'],r['zero'],r['excluded'],r['pending']),(8,5,2,3,0));self.assertEqual(r['rate'],2/5)
        data={r['sku']:r for r in service.detail(self.repo,self.options)['rows']};self.assertEqual(data['002']['stock'],-3)
        self.assertEqual(data['006']['reason'],'商品标签日配');self.assertFalse(data['007']['excluded']);self.assertFalse(data['008']['excluded']);self.assertFalse(self.calls)
    def test_each_warehouse_separate_and_multidimensional_filter(self):
        self.ingest();self.ingest([['001','同码河北',10,'启用','','','饮料','S3','供货三']],wh='河北仓')
        groups=service.cuts(self.repo,self.options,['warehouse','supplier','category'])
        self.assertIn('supplier_code',groups['dimensions']);self.assertEqual(sum(r['base'] for r in groups['rows']),6)
        opt={**self.options,'filters':{'warehouse':['山东仓'],'supplier':['供货一'],'category':['饮料']}}
        r=service.query(self.repo,opt);self.assertEqual((r['base'],r['zero'],r['rate']),(4,2,.5))
        self.assertEqual(service.filter_values(self.repo,opt,'supplier')['values'],['供货一'])
        self.assertEqual(service.query(self.repo,{**opt,'filters':{'warehouse':['山东仓'],'supplier':['供货一','供货二']}})['base'],5)
        keys=service.cuts(self.repo,opt,['sku'])['dimensions'];self.assertEqual(keys,['sku','name'])
    def test_missing_classification_or_stock_is_pending_not_zero(self):
        self.ingest([['x','缺库存',None,'启用','','','食品','S','供货']]);r=service.query(self.repo,self.options)
        self.assertEqual(r['pending'],1);self.assertIsNone(r['rate']);self.assertEqual(r['zero'],0)
        headers=[h for h in HEADERS if h!='商品标签'];rows=[[v for i,v in enumerate(row) if HEADERS[i]!='商品标签'] for row in ROWS]
        self.ingest(rows,headers);r=service.query(self.repo,self.options);self.assertGreater(r['pending'],0);self.assertIsNone(r['rate'])
    def test_known_exclusion_does_not_require_unknown_stock(self):
        self.ingest([['x','禁用未知库存',None,'禁用',None,None,None,None,None]])
        r=service.query(self.repo,self.options);self.assertEqual((r['base'],r['excluded'],r['pending']),(0,1,0));self.assertIsNone(r['rate'])
    def test_duplicate_sku_or_used_header_keeps_prior_publication(self):
        self.ingest();before=service.query(self.repo,self.options)
        for rows,headers in [(ROWS+[ROWS[0]],HEADERS),(ROWS,HEADERS+['商品标签'])]:
            with self.assertRaises((sqlite3.IntegrityError,ValueError)):self.ingest(rows,headers)
            self.assertEqual(service.query(self.repo,self.options),before)
    def test_original_recalculation_no_network_and_immutable_observation(self):
        rid,path=self.ingest();before=service.query(self.repo,self.options);path.unlink()
        result=execute(self.repo,'recalculate',{'run':rid},self.cancel,lambda _:None,self.rpc)
        self.assertNotEqual(result['run'],rid);self.assertEqual(service.query(self.repo,self.options),before);self.assertFalse(self.calls)
    def test_cross_day_receipt_cannot_relabel_inventory_as_another_day(self):
        _,path=self.ingest();before=service.query(self.repo,self.options)
        rid=self.repo.new_run(['山东仓'],day='2000-01-01')
        with self.assertRaisesRegex(ValueError,'跨日'):
            service.load_master(self.repo,path,'山东仓',run=rid,observed=today()+'T12:00:00+08:00')
        self.assertEqual(service.query(self.repo,self.options),before)
        with self.repo.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM snapshots WHERE day=?',('2000-01-01',)).fetchone()[0],0)
    def test_cancel_does_not_publish_or_remove_existing_results(self):
        self.ingest();before=service.query(self.repo,self.options);self.cancel.set()
        with self.assertRaises(Exception):self.ingest()
        self.assertEqual(service.query(self.repo,self.options),before)
        self.assertEqual(len(service.detail(self.repo,self.options)['rows']),8)
    def test_data_delete_cas_cleanup_and_other_module_untouched(self):
        marker=self.root/'workbench.sqlite3';marker.write_bytes(b'untouched-business');store=self.root/'analysis'/'stockout';store.mkdir();(store/'facts').write_bytes(b'untouched-store')
        rid,_=self.ingest();plan=preview(self.repo,today(),today());self.ingest(wh='河北仓')
        with self.assertRaisesRegex(ValueError,'变化'):remove(self.repo,plan)
        plan=preview(self.repo,today(),today());result=remove(self.repo,plan)
        self.assertEqual(result['pending'],0);self.assertEqual(service.query(self.repo,self.options)['total'],0);self.assertEqual(marker.read_bytes(),b'untouched-business');self.assertEqual((store/'facts').read_bytes(),b'untouched-store')
        self.assertEqual(list((self.repo.folder/'originals').glob('*.xlsx')),[])
        with self.assertRaises(ValueError):execute(self.repo,'recalculate',{'run':rid},self.cancel,lambda _:None,self.rpc)
    def test_delete_log_keeps_current_rate_and_blocks_unresolved_log(self):
        rid,_=self.ingest();before=service.query(self.repo,self.options);run=self.repo.run(rid)
        execute(self.repo,'delete_run_log',{'run':rid,'updated':run['updated']},self.cancel,lambda _:None,self.rpc)
        self.assertFalse(execute(self.repo,'open',{},self.cancel,lambda _:None,self.rpc)['runs']);self.assertEqual(service.query(self.repo,self.options),before)
        unresolved=self.repo.new_run(['山东仓']);self.repo.change(unresolved,state='PARTIAL')
        with self.assertRaisesRegex(ValueError,'未决'):execute(self.repo,'delete_run_log',{'run':unresolved,'updated':self.repo.run(unresolved)['updated']},self.cancel,lambda _:None,self.rpc)
    def test_export_all_combinations_numeric_rates_and_codes(self):
        self.ingest();target=self.root/'report.xlsx';export(self.repo,self.options,target,defaults()['combinations'])
        book=openpyxl.load_workbook(target,read_only=False,data_only=True)
        try:
            self.assertIn('大仓总览1',book.sheetnames);sheet=book['大仓总览1'];self.assertEqual(sheet['D2'].value,.4);self.assertEqual(sheet['D2'].number_format,'0.00%')
            rows=list(book['商品明细1'].values);self.assertEqual(len(rows),9);self.assertEqual(rows[1][2],'001');self.assertEqual(sum(r[15] for r in rows[1:]),3)
            self.assertEqual(book['商品明细1']['C2'].data_type,'s');self.assertIsNotNone(sheet.auto_filter.ref)
        finally:book.close()
        with self.assertRaises(ValueError):export(self.repo,self.options,target,defaults()['combinations'])
    def test_date_and_filter_boundaries_no_phantom_days(self):
        self.ingest();result=service.query(self.repo,{'start':'2026-11-01','end':'2026-11-30'})
        self.assertEqual(len(result['coverage']),1)
        for opts in ({'start':'2026-01-01','end':'2028-01-01'},{**self.options,'filters':{'raw_sql':['1']}}):
            with self.assertRaises(ValueError):service.query(self.repo,opts)


if __name__=='__main__':unittest.main()
