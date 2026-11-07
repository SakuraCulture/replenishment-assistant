"""Isolated store-stockout contracts. No user files or live platforms."""
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import openpyxl
from stockout_repository import Repository
from stockout_facts import load_catalog,load_store
from stockout_common import file_hash,atomic_json
import stockout_metrics as metrics


def workbook(path,headers,rows):
    book=openpyxl.Workbook();ws=book.active;ws.append(headers)
    for row in rows:ws.append(row)
    book.save(path);book.close();return path


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.repo=Repository(self.root)
        self.repo.initialize()
        self.settings=self.repo.save_settings({'stores':[{'code':'S','name':'门店S'}],
            'routes':{'S':{'primary':'山东仓','secondary':None}},'profile':None})

    def sales(self,rows):
        return workbook(self.root/'sales.xlsx',['仓库/门店编码','商品编码','商品名称','月销','可用库存','在途库存'],
                        [['S',sku,sku,m,k,6] for sku,m,k in rows])

    def catalog(self,rows,warehouse='山东仓',tag_column=True):
        heads=['商品编码','商品状态','可用数','分类','供应商名称']+(['虚拟分类'] if tag_column else [])
        path=workbook(self.root/(warehouse+'.xlsx'),heads,rows)
        return load_catalog(self.repo,path,warehouse)

    def publish(self,rows,catalogs=None):
        run=self.repo.new_run(day='2026-11-07')
        self.repo.change(run['id'],catalogs=catalogs or {})
        load_store(self.repo,run['id'],'S',self.sales(rows),observed='2026-11-07T08:00:00+08:00')
        self.repo.publish(run['id'])
        return run['id']

    def total(self,view='all',filters=None):
        return metrics.query(self.repo,{'start':'2026-11-07','view':view,'filters':filters or {}})['total']

    def test_no_io_until_initialize(self):
        folder=self.root/'unused';repo=Repository(folder)
        self.assertFalse(folder.exists());self.assertFalse(repo.path.exists())

    def test_native_v2_three_unused_margin_headers_are_accepted(self):
        import json
        contract=json.loads((Path(__file__).resolve().parents[1]/'components/aoxiang/assets/field-contract-v2.json').read_text('utf-8'))
        heads=[c['rawHeader'] for c in contract['columns']]
        self.assertEqual(heads.count('毛利'),3)
        values={'仓库/门店编码':'S','商品编码':'A','商品名称':'原生商品','月销':5,'可用库存':0,'在途库存':6}
        source=workbook(self.root/'native-v2.xlsx',heads,[[values.get(h,100+i if h=='毛利' else None) for i,h in enumerate(heads)]])
        before=file_hash(source);run=self.repo.new_run(day='2026-11-07')
        load_store(self.repo,run['id'],'S',source,observed='2026-11-07T08:00:00+08:00',expected=before,expected_rows=1)
        self.repo.publish(run['id'])
        self.assertEqual((self.total()['base'],self.total()['zero']),(1,1));self.assertEqual(file_hash(source),before)
        with self.repo.connect() as db:self.assertEqual(tuple(db.execute('SELECT month,stock,inbound FROM facts').fetchone()),(5,0,6))

    def test_repeated_consumed_headers_name_columns_and_never_guess(self):
        from stockout_facts import table,STORE_FIELDS
        for header in STORE_FIELDS:
            with self.subTest(header=header):
                heads=list(STORE_FIELDS)+[' '+header+' ']
                source=workbook(self.root/'bad-heads.xlsx',heads,[['S','A','商品',1,0,0,'食品','正常',99]])
                with self.assertRaisesRegex(ValueError,'分析使用的表头重复：'+header):
                    with table(source,['仓库/门店编码','商品编码','月销','可用库存'],used=STORE_FIELDS):pass

    def test_catalog_optional_tag_or_supplier_duplicates_remain_blocked(self):
        for header in ['虚拟分类','供应商名称','可用数']:
            with self.subTest(header=header):
                source=workbook(self.root/'catalog-duplicate.xlsx',['商品编码',header,header],[['A','first','last']])
                with self.assertRaisesRegex(ValueError,'分析使用的表头重复：'+header):load_catalog(self.repo,source,'山东仓')

    def test_duplicate_unused_columns_cannot_override_consumed_values(self):
        from stockout_facts import table,STORE_FIELDS
        source=workbook(self.root/'duplicates.xlsx',['仓库/门店编码','商品编码','月销','可用库存','毛利','毛利'],[['S','A',7,0,12,99]])
        with table(source,['仓库/门店编码','商品编码','月销','可用库存'],used=STORE_FIELDS) as (_,rows):
            value=list(rows)[0][1]
        self.assertEqual(value,{'仓库/门店编码':'S','商品编码':'A','月销':7,'可用库存':0})

    def test_base_stock_zero_and_inbound(self):
        self.publish([('A',3,0),('B',4,1),('C',0,0)])
        value=self.total()
        self.assertEqual((value['base'],value['zero'],value['rate']),(2,1,.5))
        detail=metrics.detail(self.repo,{'start':'2026-11-07'},only_zero=True)
        self.assertEqual(detail['total'],1)
        self.assertEqual(self.total()['base'],2)

    def test_tags_four_views_union_and_no_fuzzy_match(self):
        sha=self.catalog([['A','启用',2,'饮品','供货商','标杆淘汰（夏季）'],
                          ['B','禁用',0,'饮品','供货商','淘便考核'],
                          ['C','备用',0,'食品','供货商','标杆淘汰；淘便考核'],
                          ['D','启用',3,'食品','供货商','待淘汰,不淘汰'],
                          ['E','启用',3,'食品','供货商','']])
        self.publish([(s,1,0 if s in 'ACD' else 1) for s in 'ABCDE'],{'山东仓':sha})
        expected={'all':(5,3),'retired':(3,1),'assessment':(3,2),'both':(2,1)}
        for view,pair in expected.items():
            with self.subTest(view=view):
                value=self.total(view);self.assertEqual((value['base'],value['zero']),pair)
        value=self.total('both');self.assertEqual((value['removed_base'],value['removed_zero']),(3,2))

    def test_unknown_tags_retained_and_empty_known(self):
        sha=self.catalog([['A','启用',0,'饮品','供货商','']])
        self.publish([('A',1,0),('B',1,0)],{'山东仓':sha})
        self.assertEqual(self.total('both')['base'],2)
        self.assertEqual(self.total('both')['unknown_tags'],1)

    def test_single_warehouse_other_catalog_does_not_steal_local(self):
        sd=self.catalog([['A','启用',0,'饮品','山东供货商','']])
        hb=self.catalog([['B','启用',3,'食品','河北供货商','淘便考核']],'河北仓')
        self.publish([('B',1,0)],{'山东仓':sd,'河北仓':hb})
        row=metrics.detail(self.repo,{'start':'2026-11-07'})['items'][0]
        self.assertEqual(row['source'],'local');self.assertIsNone(row['assessment'])
        self.assertEqual(self.total('both')['base'],1)

    def test_missing_selected_catalog_pending_not_local(self):
        self.publish([('A',1,0)])
        row=metrics.detail(self.repo,{'start':'2026-11-07'})['items'][0]
        self.assertEqual(row['source'],'pending');self.assertEqual(self.total()['rate'],1)

    def test_invalid_metric_values_never_become_zero(self):
        for month,stock in [(1,-1),(1,None),(-1,0),(None,0),('=1+1',0)]:
            with self.subTest(month=month,stock=stock):
                self.publish([('A',month,stock)])
                self.assertIsNone(self.total()['rate']);self.assertEqual(self.total()['bad'],1)

    def test_zero_month_bad_stock_is_not_metric_blocker(self):
        self.publish([('A',0,None),('B',2,1)])
        self.assertEqual(self.total()['rate'],0)

    def test_zero_base_dash(self):
        self.publish([('A',0,0)])
        self.assertIsNone(self.total()['rate']);self.assertEqual(self.total()['base'],0)

    def test_duplicate_and_other_store_are_rejected(self):
        for rows in [[['S','A',1,0],['S','A',1,0]],[['T','A',1,0]]]:
            rid=self.repo.new_run()['id']
            path=workbook(self.root/'bad.xlsx',['仓库/门店编码','商品编码','月销','可用库存'],rows)
            with self.assertRaises(ValueError):load_store(self.repo,rid,'S',path,observed='2026-11-07T08:00:00+08:00')
            with self.assertRaises(ValueError):self.repo.publish(rid)
            self.repo.change(rid,state='ENDED')

    def test_expected_rows_and_source_hash_preserved(self):
        rid=self.repo.new_run()['id'];path=self.sales([('A',2,0)]);before=file_hash(path)
        with self.assertRaises(ValueError):load_store(self.repo,rid,'S',path,observed='2026-11-07T08:00:00+08:00',expected_rows=2)
        self.assertEqual(file_hash(path),before)

    def test_published_revision_immutable_and_same_file_no_duplicate(self):
        rid=self.publish([('A',1,0)])
        with self.assertRaises(ValueError):self.repo.change(rid,message='覆盖')
        new=self.repo.new_run(source_run=rid)
        load_store(self.repo,new['id'],'S',self.root/'sales.xlsx',observed='2026-11-07T08:00:00+08:00')
        self.repo.publish(new['id'])
        with self.repo.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM facts').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM publications').fetchone()[0],2)

    def test_local_adoption_does_not_write_defaults(self):
        atomic_json(self.root/'store_supply.json',{'S':{'primary':'河北仓','secondary':None}})
        sha=file_hash(self.root/'store_supply.json')
        self.repo.adopt_business([{'code':'S','name':'门店S'}])
        self.assertEqual(file_hash(self.root/'store_supply.json'),sha)
        self.assertFalse((self.root/'must_sell.json').exists())
        frozen=self.repo.settings()
        atomic_json(self.root/'store_supply.json',{'S':{'primary':'山东仓','secondary':None}})
        self.assertEqual(self.repo.settings(),frozen)

    def test_cross_midnight_real_observation_date(self):
        rid=self.repo.new_run(day='2026-11-07')['id']
        load_store(self.repo,rid,'S',self.sales([('A',1,0)]),observed='2026-11-08T00:01:00+08:00')
        self.repo.publish(rid)
        self.assertEqual(metrics.dates(self.repo),['2026-11-08'])

    def test_cuts_and_summary_share_filter(self):
        sha=self.catalog([['A','启用',0,'饮品','供应商甲',''],['B','启用',0,'食品','供应商乙','']])
        self.publish([('A',1,0),('B',1,1)],{'山东仓':sha})
        options={'start':'2026-11-07','filters':{'category':['饮品']}}
        self.assertEqual(metrics.query(self.repo,options)['total']['rate'],1)
        cuts=metrics.cuts(self.repo,options,('supplier','category'))
        self.assertEqual(cuts['total'],1);self.assertEqual(cuts['items'][0]['zero'],1)

    def test_identity_names_supplement_codes_without_splitting_groups(self):
        self.publish([('A',1,0),('B',1,1)])
        self.publish([('A',1,0),('B',1,1)])
        with self.repo.connect(write=True) as db:
            db.execute("UPDATE fact_records SET name='相同商品名'")
        rows=metrics.cuts(self.repo,{'start':'2026-11-07'},('store','sku'))['items']
        self.assertEqual(len(rows),2)
        self.assertEqual({r['sku'] for r in rows},{'A','B'})
        self.assertEqual({r['sku_name'] for r in rows},{'相同商品名'})
        self.assertEqual({r['store_name'] for r in rows},{'门店S'})
        self.assertEqual(sum(r['base'] for r in rows),2)
        chosen=metrics.query(self.repo,{'start':'2026-11-07','filters':{'sku':['A']}})['total']
        self.assertEqual((chosen['base'],chosen['zero']),(1,1))

    def test_multiple_names_for_one_code_keep_one_group_and_show_notice(self):
        self.repo.save_settings({'stores':[{'code':s,'name':'同名门店'} for s in ('S','T')],'routes':{},'profile':None})
        rid=self.repo.new_run()['id']
        for store,name in [('S','旧名称'),('T','新名称')]:
            path=workbook(self.root/(store+'.xlsx'),['仓库/门店编码','商品编码','商品名称','月销','可用库存'],[[store,'A',name,1,0]])
            load_store(self.repo,rid,store,path,observed='2026-11-07T08:00:00+08:00')
        self.repo.publish(rid)
        self.assertEqual(metrics.cuts(self.repo,{'start':'2026-11-07'},('store',))['total'],2)
        cut=metrics.cuts(self.repo,{'start':'2026-11-07'},('sku',))
        self.assertEqual(cut['total'],1);self.assertEqual(cut['items'][0]['base'],2)
        self.assertIn('同编码多名称',cut['items'][0]['sku_name'])

    def test_precise_unsafe_sku_is_rejected(self):
        rid=self.repo.new_run()['id']
        with self.assertRaises(ValueError):load_store(self.repo,rid,'S',self.sales([(12345678901234567,1,0)]),observed='2026-11-07T08:00:00+08:00')

    def test_weighted_two_stores_and_normalized_dimensions(self):
        self.repo.save_settings({'stores':[{'code':s,'name':s} for s in ('S','T')],'routes':{},'profile':None})
        rid=self.repo.new_run()['id']
        for store,rows in [('S',[(1,0)]),('T',[(1,0),(1,1),(1,1)])]:
            path=workbook(self.root/(store+'.xlsx'),['仓库/门店编码','商品编码','月销','可用库存'],[[store,str(i),m,k] for i,(m,k) in enumerate(rows)])
            load_store(self.repo,rid,store,path,observed='2026-11-07T08:00:00+08:00')
        self.repo.publish(rid)
        self.assertEqual(self.total()['rate'],.5)
        with self.repo.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM fact_dimensions').fetchone()[0],1)

    def test_state_and_tag_dimensions_keep_valid_denominator(self):
        sha=self.catalog([['A','禁用',0,'饮品','供货商','']])
        self.publish([('A',2,0)],{'山东仓':sha})
        for dimension,value in [('product_status','禁用'),('inbound_status','有在途'),('tag_status','标签已取得')]:
            cut=metrics.cuts(self.repo,{'start':'2026-11-07'},[dimension])['items'][0]
            self.assertEqual(cut[dimension],value);self.assertEqual(cut['base'],1)

    def test_cleanup_scope_stale_preview_cancel_and_shared_original(self):
        from stockout_maintenance import preview,execute
        from runtime_support import Cancelled
        import threading
        rid=self.publish([('A',1,0)]);source=self.root/'sales.xlsx'
        run=self.repo.new_run()['id'];load_store(self.repo,run,'S',source,observed='2026-11-08T08:00:00+08:00');self.repo.publish(run)
        plan=preview(self.repo,'2026-11-07','2026-11-07')
        cancel=threading.Event();cancel.set()
        with self.assertRaises(Cancelled):execute(self.repo,plan,cancel)
        self.assertEqual(self.total()['base'],1)
        with self.repo.connect(write=True) as db:db.execute("UPDATE runs SET updated='changed' WHERE id=?",(rid,))
        with self.assertRaisesRegex(ValueError,'变化'):execute(self.repo,plan)
        result=execute(self.repo,preview(self.repo,'2026-11-07','2026-11-07'))
        self.assertEqual(result['pending'],0);self.assertEqual(result['deleted_originals'],0)
        self.assertEqual(metrics.dates(self.repo),['2026-11-08'])
        self.assertTrue(source.exists())
        execute(self.repo,preview(self.repo,'2026-11-08','2026-11-08'))
        self.assertTrue(source.exists());self.assertEqual(list((self.repo.folder/'originals').iterdir()),[])

    def test_export_equal_literal_strings_and_cancel_removes_temp(self):
        from stockout_exports import export
        from openpyxl.worksheet._writer import ALL_TEMP_FILES
        from runtime_support import Cancelled
        import threading
        self.publish([(str(i),1,i%2) for i in range(1005)])
        with self.repo.connect(write=True) as db:db.execute("UPDATE fact_records SET name='=1+1' WHERE sku='0'")
        path=self.root/'report.xlsx';export(self.repo,{'start':'2026-11-07'},path,include_detail=True,dimensions=('store','sku'))
        book=openpyxl.load_workbook(path)
        try:
            self.assertEqual((book['总体']['A2'].value,book['总体']['B2'].value),(1005,503))
            self.assertEqual(tuple(c.value for c in book['门店汇总'][1][:2]),('门店编码','门店名称'))
            self.assertEqual(tuple(c.value for c in book['门店汇总'][2][:2]),('S','门店S'))
            self.assertEqual(tuple(c.value for c in book['当前切面'][1][:4]),('门店编码','门店名称','商品编码','商品名称'))
            self.assertEqual(book['当前切面']['D2'].value,'=1+1');self.assertEqual(book['当前切面']['D2'].data_type,'s')
            self.assertEqual(book['商品明细1']['E2'].data_type,'s');self.assertEqual(book['商品明细1']['E2'].value,'=1+1')
            self.assertIn('剔除与标签待核',book.sheetnames)
        finally:book.close()
        with self.assertRaises(ValueError):export(self.repo,{'start':'2026-11-07'},path)
        baseline=set(ALL_TEMP_FILES);cancel=threading.Event()
        with self.assertRaises(Cancelled):
            export(self.repo,{'start':'2026-11-07'},self.root/'cancel.xlsx',include_detail=True,cancel=cancel,progress=lambda _:cancel.set())
        self.assertFalse((self.root/'cancel.xlsx').exists());self.assertEqual(set(ALL_TEMP_FILES),baseline)

    def test_page_byte_budget_and_low_resource_pause(self):
        from stockout_common import resource_check
        from runtime_support import Cancelled
        from unittest.mock import patch
        from types import SimpleNamespace
        rows=metrics.bounded_page([{'value':'a'*1000} for _ in range(200)],budget=2500)
        self.assertEqual(len(rows),2)
        with self.assertRaises(Cancelled):resource_check(self.root,memory_available=1)
        with patch('shutil.disk_usage',return_value=SimpleNamespace(free=1)):
            with self.assertRaises(Cancelled):resource_check(self.root)

    def test_aggregates_equal_raw_facts_for_all_views_and_filters(self):
        from unittest.mock import patch
        sha=self.catalog([[s,'禁用' if s=='C' else '启用',0,'类目'+str(i%2),'供应商'+str(i%3),
                           ['标杆淘汰','淘便考核','标杆淘汰;淘便考核',''][i%4]] for i,s in enumerate('ABCDEFGH')])
        self.publish([(s,1 if i%3 else 0,0 if i%2 else 1) for i,s in enumerate('ABCDEFGHI')]+[('X',None,0),('Y',1,None)],{'山东仓':sha})
        for view in ('all','retired','assessment','both'):
            for filters in ({},{'source':['center']},{'category':['类目0','类目1']},{'tag_status':['标签待核']}):
                q={'start':'2026-11-07','view':view,'filters':filters}
                fast=metrics.cuts(self.repo,q,['source','category','mandatory'])
                details=metrics.detail(self.repo,q,only_zero=True)
                with patch.object(metrics,'fact_source',return_value='facts'):
                    self.assertEqual(fast,metrics.cuts(self.repo,q,['source','category','mandatory']))
                    self.assertEqual(details,metrics.detail(self.repo,q,only_zero=True))
        whole=metrics.detail(self.repo,{'start':'2026-11-07'})
        records=[];offset=0
        while offset is not None:
            page=metrics.detail(self.repo,{'start':'2026-11-07'},limit=2,offset=offset)
            records.extend(page['items']);offset=page['next_offset']
        self.assertEqual(records,whole['items'])


class RoutingEquivalenceTests(unittest.TestCase):
    def test_selection_exhaustive_matches_previous_expression(self):
        from warehouse_routing import select_warehouse
        for secondary in (None,'河北仓'):
            for state in ('禁用','无货','无该商品','可用数待核','有货'):
                for pm in ('商品编码精确匹配','资料未匹配','编码待核'):
                    for sm in ('商品编码精确匹配','资料未匹配','编码待核'):
                        target='山东仓'
                        if state in {'禁用','无货','无该商品'} and secondary:target=secondary
                        retain=(target!='山东仓' and pm=='商品编码精确匹配' and sm=='资料未匹配')
                        if retain:target='山东仓'
                        self.assertEqual(select_warehouse('山东仓',secondary,state,pm,sm),(target,retain))


if __name__=='__main__':unittest.main()
