"""Flexible cuts and exports remain equivalent to the isolated, frozen fact rows."""
from copy import deepcopy
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
import openpyxl
import test_stockout_analysis as fixture
import stockout_metrics as metrics
import stockout_views as views
from stockout_common import file_hash
from stockout_exports import export
from runtime_support import Cancelled


class ViewTests(unittest.TestCase):
    setUp=fixture.AnalysisTests.setUp
    sales=fixture.AnalysisTests.sales
    catalog=fixture.AnalysisTests.catalog
    publish=fixture.AnalysisTests.publish
    q={'start':'2026-11-07'}

    def sample(self):
        sha=self.catalog([[s,'禁用' if s=='B' else '启用',10,'食品' if s in 'AB' else '饮品','供应商'+str(i%2),''] for i,s in enumerate('ABCD')])
        self.publish([(s,2,-1 if s in 'ACL' else 4) for s in 'ABCDL'],{'山东仓':sha})

    def test_all_twelve_dimensions_conserve_counts_and_names(self):
        self.sample();dims=list(metrics.DIMENSIONS);q={**self.q,'exclude_disabled':True}
        rows=list(metrics.iter_cuts(self.repo,q,dims));total=metrics.query(self.repo,q)['total']
        self.assertEqual(len(rows),5)
        for key in metrics.AGG_KEYS:self.assertEqual(sum(r[key] for r in rows),total[key])
        self.assertEqual(metrics.cuts(self.repo,q,dims)['items'],rows)
        self.assertTrue(all(r['store']=='S' and r['store_name']=='门店S' and r['sku_name']==r['sku'] and r['day']=='2026-11-07' for r in rows))
        self.assertEqual((total['base'],total['zero'],total['rate']),(4,3,.75))

    def test_multiselect_uses_or_within_dimension_and_and_across_dimensions(self):
        self.sample();q={**self.q,'filters':{'category':['食品','饮品'],'supplier':['山东仓 · 供应商0'],'sku':['A','B','C']}}
        total=metrics.query(self.repo,q)['total'];rows=metrics.detail(self.repo,q)['items']
        self.assertEqual({r['sku'] for r in rows},{'A','C'})
        self.assertEqual((total['base'],total['zero'],total['rate']),(2,2,1))
        self.assertEqual(metrics.trend(self.repo,q)[0]['total'],total)
        self.assertEqual(sum(r['base'] for r in metrics.iter_cuts(self.repo,q,['store','warehouse','category','supplier','mandatory'])),2)

    def test_legacy_local_label_normalized_without_snapshot_mutation(self):
        self.sample()
        with self.repo.connect(write=True) as db:db.execute("UPDATE fact_dimensions SET warehouse='不适用' WHERE source='local'")
        before=file_hash(self.repo.path)
        for warehouse in ('地采','不适用'):
            q={**self.q,'filters':{'warehouse':[warehouse]}}
            self.assertEqual(metrics.query(self.repo,q)['total']['base'],1)
            self.assertEqual(metrics.cuts(self.repo,q,['source','warehouse'])['items'][0]['warehouse'],'地采')
            self.assertEqual(metrics.detail(self.repo,q)['items'][0]['warehouse'],'地采')
        choices=metrics.filter_values(self.repo,self.q,'warehouse')['items']
        self.assertEqual({r['warehouse'] for r in choices},{'地采','山东仓'})
        target=self.root/'local.xlsx';export(self.repo,self.q,target,include_detail=True,combinations=[{'name':'仓','dimensions':['warehouse']}])
        book=openpyxl.load_workbook(target,read_only=True)
        try:
            self.assertEqual({r[0] for r in list(book['仓'].values)[1:]},{'山东仓','地采'})
            rows=list(book['商品明细1'].values);header=rows[0]
            local=next(dict(zip(header,r)) for r in rows[1:] if r[3]=='L');self.assertEqual(local['归属仓'],'地采')
        finally:book.close()
        self.assertEqual(file_hash(self.repo.path),before)

    def test_filter_values_search_names_escape_wildcards_and_remove_only_own_filter(self):
        self.sample()
        with self.repo.connect(write=True) as db:db.execute("UPDATE fact_records SET name='名字%_\\' WHERE sku='A'")
        q={**self.q,'filters':{'sku':['B'],'category':['食品']}}
        before=deepcopy(q)
        rows=metrics.filter_values(self.repo,q,'sku')['items']
        self.assertEqual({r['sku'] for r in rows},{'A','B'});self.assertEqual(q,before)
        self.assertEqual([r['sku'] for r in metrics.filter_values(self.repo,q,'sku',search='%_\\')['items']],['A'])
        self.assertEqual(metrics.filter_values(self.repo,q,'sku',search="' OR 1=1 --")['items'],[])
        self.assertEqual(metrics.filter_values(self.repo,self.q,'store',search='门店S')['items'][0]['store'],'S')
        self.assertEqual(metrics.filter_values(self.repo,self.q,'source',search='地采')['items'][0]['source'],'local')

    def test_filter_candidate_pagination_has_no_missing_or_duplicate_codes(self):
        self.publish([(f'{i:04}',1,i%2) for i in range(407)])
        offset=0;found=[]
        while True:
            value=metrics.filter_values(self.repo,self.q,'sku',offset=offset)
            self.assertLessEqual(len(value['items']),200);self.assertEqual(value['total'],407)
            found.extend(r['sku'] for r in value['items']);offset=value['next_offset']
            if offset is None:break
        self.assertEqual(found,[f'{i:04}' for i in range(407)])
        q={**self.q,'filters':{'sku':found}}
        self.assertEqual(metrics.query(self.repo,q)['total']['base'],407)

    def test_nullable_dimension_filters_remain_explicit(self):
        self.sample();q={**self.q,'filters':{'mandatory':[None]}}
        self.assertEqual(metrics.query(self.repo,q)['total']['base'],5)
        self.assertEqual(metrics.filter_values(self.repo,self.q,'mandatory')['items'][0]['mandatory'],None)

    def test_day_multiselect_filters_coverage_trend_details_and_export_evidence(self):
        self.publish([('A',1,0)])
        run=self.repo.new_run(day='2026-11-08')
        fixture.load_store(self.repo,run['id'],'S',self.sales([('A',1,2)]),observed='2026-11-08T08:00:00+08:00');self.repo.publish(run['id'])
        q={**self.q,'end':'2026-11-08','filters':{'day':['2026-11-08']}}
        result=metrics.query(self.repo,q)
        self.assertEqual((result['total']['base'],result['total']['zero']),(1,0))
        self.assertEqual([r['day'] for r in result['coverage']],['2026-11-08'])
        self.assertEqual([r['day'] for r in metrics.trend(self.repo,q)],['2026-11-08'])
        self.assertEqual([r['day'] for r in metrics.detail(self.repo,q)['items']],['2026-11-08'])
        target=self.root/'day.xlsx';export(self.repo,q,target)
        book=openpyxl.load_workbook(target,read_only=True)
        try:
            self.assertEqual([r[0] for r in list(book['快照依据'].values)[1:]],['2026-11-08'])
            self.assertEqual([r[0] for r in list(book['日报覆盖'].values)[1:]],['2026-11-08'])
        finally:book.close()

    def test_view_preferences_are_separate_atomic_and_guard_concurrent_edits(self):
        before=file_hash(self.repo.path);settings=self.repo.settings();initial=views.load(self.repo)
        self.assertFalse((self.repo.folder/'views.json').exists())
        value=deepcopy(initial);value['active']=list(metrics.DIMENSIONS)
        value['combinations'].append({'name':'全维度','dimensions':value['active']})
        saved=views.save(self.repo,value,initial['revision']);self.assertEqual(views.load(self.repo),saved)
        with self.assertRaisesRegex(ValueError,'其他窗口'):views.save(self.repo,initial,initial['revision'])
        bad=deepcopy(saved);bad['combinations'][1]['name']=bad['combinations'][0]['name']
        with self.assertRaisesRegex(ValueError,'重复'):views.save(self.repo,bad,saved['revision'])
        self.assertEqual(views.load(self.repo),saved);self.assertEqual(self.repo.settings(),settings)
        self.assertEqual(file_hash(self.repo.path),before)

    def test_export_custom_sheets_include_all_groups_and_real_excel_filters(self):
        self.publish([(f'{i:04}',1,i%3) for i in range(407)])
        with self.repo.connect(write=True) as db:db.execute("UPDATE fact_records SET name='=1+1' WHERE sku='0000'")
        target=self.root/'custom.xlsx';combos=[{'name':'六维交叉','dimensions':['store','sku','warehouse','category','supplier','mandatory']},
                                               {'name':'门店日期','dimensions':['day','store']}]
        # The export must not call the bounded/paginated UI path.
        with patch.object(metrics,'cuts',side_effect=AssertionError('Do not repeatedly regroup by UI page')):
            export(self.repo,self.q,target,combinations=combos)
        book=openpyxl.load_workbook(target)
        try:
            ws=book['六维交叉'];rows=list(ws.values);header=rows[0]
            self.assertEqual(ws.max_row,408)
            self.assertEqual(header[:8],('门店编码','门店名称','商品编码','商品名称','供货仓','商品类目','仓库上游供货商','必采属性'))
            self.assertEqual(ws.freeze_panes,'A2');self.assertEqual(ws.auto_filter.ref,'A1:Q408')
            self.assertEqual(len({r[2] for r in rows[1:]}),407)
            self.assertEqual(sum(r[header.index('有销基数')] for r in rows[1:]),407)
            self.assertEqual(sum(r[header.index('缺货数')] for r in rows[1:]),136)
            self.assertEqual(ws['D2'].value,'=1+1');self.assertEqual(ws['D2'].data_type,'s')
            daily=list(book['门店日期'].values);self.assertEqual(daily[1][:5],('2026-11-07','S','门店S',407,136))
            self.assertEqual(book['门店日期']['F2'].number_format,'0.00%')
            self.assertNotIn('当前切面',book.sheetnames)
        finally:book.close()

    def test_excel_safe_sheet_names_and_overflow_keep_every_group(self):
        self.publish([(str(i),1,0) for i in range(8)])
        combos=[{'name':'总体','dimensions':['sku']},{'name':'a/b','dimensions':['sku']},{'name':'a?b','dimensions':['store']}]
        path=self.root/'parts.xlsx'
        with patch('stockout_exports.SHEET_ROWS',3):export(self.repo,self.q,path,combinations=combos)
        book=openpyxl.load_workbook(path)
        try:
            names=book.sheetnames;self.assertEqual(len(names),len({n.casefold() for n in names}))
            sheets=[s for s in book if s['A1'].value=='商品编码']
            self.assertEqual(sum(s.max_row-1 for s in sheets),16)
            self.assertTrue(all(s.max_row<=4 and s.auto_filter.ref for s in sheets))
            self.assertTrue(any(n.startswith('a_b') for n in names))
        finally:book.close()

    def test_export_cancellation_mid_cut_removes_only_new_temporaries(self):
        from openpyxl.worksheet._writer import ALL_TEMP_FILES
        self.publish([(str(i),1,0) for i in range(1005)]);cancel=threading.Event()
        target=self.root/'cancel.xlsx';temps=set(ALL_TEMP_FILES);before=file_hash(self.repo.path)
        def progress(message):
            if '1000 组' in message:cancel.set()
        with self.assertRaises(Cancelled):export(self.repo,self.q,target,combinations=[{'name':'大切面','dimensions':['sku']}],cancel=cancel,progress=progress)
        self.assertFalse(target.exists());self.assertEqual(set(ALL_TEMP_FILES),temps)
        self.assertEqual(list(self.root.glob('.stockout-*.xlsx')),[]);self.assertEqual(file_hash(self.repo.path),before)

    def test_bad_dimensions_and_filter_values_rejected_without_export(self):
        self.sample()
        for dims in ([],['store','store'],['evil'],[{}],None,list(metrics.DIMENSIONS)+['store']):
            with self.subTest(dims=dims),self.assertRaises(ValueError):metrics.cuts(self.repo,self.q,dims)
        for filters in ({'sku':[{}]},{'sku':list(map(str,range(1001)))},{'unknown':['x']}):
            with self.subTest(filters=str(filters)[:40]),self.assertRaises(ValueError):metrics.query(self.repo,{**self.q,'filters':filters})
        target=self.root/'bad.xlsx'
        with self.assertRaises(ValueError):export(self.repo,self.q,target,combinations=[])
        self.assertFalse(target.exists())
        from stockout_worker import execute
        (self.repo.folder/'views.json').write_text('{"version":1,"active":[{}],"combinations":[]}',encoding='utf-8')
        opened=execute(self.repo,'open',{},None,lambda _:None,lambda *a,**k:self.fail('No platform call for view preferences'))
        self.assertTrue(opened['views_error']);self.assertEqual(opened['settings']['version'],self.repo.settings()['version'])
