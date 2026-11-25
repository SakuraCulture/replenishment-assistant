"""Operating report rules, workbook formulas and frozen source identity."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import openpyxl

from business_daily import PendingInputs, aggregate, classify
import business_daily_sources as sources
from business_daily_sources import file_source, path_for, sales_rows
from business_daily_workbook import SHEETS, write


def product(stock, status='启用', virtual='', category='食品', supplier='测试供货商'):
    return {'stock': stock, 'status': status, 'virtual': virtual, 'category': category, 'supplier': supplier}


def fixture():
    stores = {
        'A': {'code': 'A', 'name': '测试A', 'tag': '山东-维稳型', 'owner': '甲',
              'primary': '山东仓', 'secondary': None},
        'B': {'code': 'B', 'name': '测试B', 'tag': '河北-维稳型', 'owner': '乙',
              'primary': '河北仓', 'secondary': None},
        'C': {'code': 'C', 'name': '测试C', 'tag': '河北-新店', 'owner': '乙',
              'primary': '河北仓', 'secondary': '山东仓'},
    }
    catalogs = {'山东仓': {'0001': product(0), '0002': product(2), '0003': product(3), '@01': product(5)},
                '河北仓': {'0002': product(4, '禁用'), '0003': product(0, category='日配')}}
    sales = []
    for code, sku, stock, inbound in (
            ('A', '0002', 0.5, 4), ('A', '0001', 0, 0), ('A', '@01', 1, 0),
            ('B', '0001', 0, 0), ('B', '0003', -1, 2), ('B', '0002', 0, 0),
            ('C', '0001', 0, 0), ('C', '@01', 0, 0)):
        sales.append({'store': code, 'sku': sku, 'name': sku, 'status': '正常', 'month': 3,
                      'stock': stock, 'inbound': inbound, 'source': '隔离底稿'})
    return {'stores': stores, 'catalogs': catalogs, 'sales': sales, 'blacklist': {('测试C', '@01')},
            'owners': {'食品': '测试采购'}, 'weeks': {'metrics': {}},
            'must_sell': {'A': {'rate': 0.05, 'owner': '甲', 'group': '华东', 'name': '测试A'},
                          'B': {'rate': 0.15, 'owner': '乙', 'group': '华北', 'name': '测试B'},
                          'OTHER': {'rate': 0.4, 'owner': '丙', 'group': '华东', 'name': '外表独有'}},
            'orders': [
                {'store': 'A', 'warehouse': '山东仓', 'order': 'BH-A', 'revision': 1, 'sku': '0001',
                 'demand': 12, 'demand_value': 37.8, 'ordered': 0, 'ordered_value': 0},
                {'store': 'B', 'warehouse': '河北仓', 'order': 'BH-B', 'revision': 2, 'sku': '0003',
                 'demand': 5, 'demand_value': 4.8, 'ordered': 5, 'ordered_value': 4.8},
            ]}


class DailyRules(unittest.TestCase):
    def test_supplier_routes_and_backfall(self):
        data = fixture()
        a, b, c = data['stores'].values()
        self.assertEqual(classify(a, '0001', data['catalogs'])[0], '主仓（山东）')
        self.assertIsNone(classify(b, '0001', data['catalogs'])[0])
        self.assertEqual(classify(c, '0001', data['catalogs'])[0], '从仓（山东）')
        self.assertEqual(classify(c, '0002', data['catalogs'])[0], '河北仓禁采')
        details = []
        result = aggregate(data, details.append)
        self.assertEqual(result['stockout_sizes'], {'山东仓': 1, '河北仓': 1})
        self.assertEqual([r[5] for r in details], ['0001', '0001', '0003', '0002', '0001'])
        self.assertEqual(result['counts']['A'].shortage, 2)
        self.assertEqual(result['counts']['A'].warehouse_shortage, 1)
        self.assertEqual(result['counts']['B'].local_shortage, 1)
        self.assertEqual(result['counts']['B'].active, 1)
        self.assertEqual(result['counts']['B'].warehouse_shortage, 1)
        self.assertEqual(result['discrepancies'], {('A', '可用库存大于0且小于1'): 1, ('C', '地采黑名单'): 1})

    def test_missing_expected_store_and_unknown_inventory(self):
        data = fixture()
        data['sales'][0]['stock'] = None
        with self.assertRaisesRegex(PendingInputs, '可用库存待核'):
            aggregate(data, lambda _: None)
        data = fixture()
        data['sales'] = [x for x in data['sales'] if x['store'] != 'C']
        with self.assertRaisesRegex(PendingInputs, '缺门店底稿：C'):
            aggregate(data, lambda _: None)

    def test_four_sheets_rate_cache_and_offline_formulas(self):
        data = fixture()
        frozen = {'day': '2026-11-23', 'partial': False, 'files': {}, 'expected_stores': list(data['stores']),
                  'order_snapshot': 'frozen-orders', 'order_cutoff': '2026-11-24T12:00:00+08:00',
                  'catalogs': {}, 'store_snapshots': [], 'sales': [], 'order_revisions': []}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'daily.xlsx'
            report = write(path, frozen, data, lambda _: None, None)
            self.assertEqual((report['stores'], report['details'], report['must_sell_stores']), (3, 5, 3))
            with closing(openpyxl.load_workbook(path, keep_links=False)) as formulas:
                self.assertEqual(tuple(formulas.sheetnames), SHEETS)
                self.assertEqual(formulas['门店缺货率'].freeze_panes, 'A3')
                self.assertEqual(formulas['门店缺货率'].auto_filter.ref, 'A2:T5')
                self.assertEqual(formulas['门店缺货明细'].auto_filter.ref, 'A1:Q6')
                self.assertEqual(formulas['汇总']['C20'].data_type, 'f')
                self.assertEqual(formulas['汇总']['G20'].data_type, 'f')
                self.assertEqual(formulas['门店缺货明细']['F2'].value, '0001')
            with closing(openpyxl.load_workbook(path, read_only=True, keep_links=False, data_only=True)) as values:
                self.assertAlmostEqual(values['汇总']['C20'].value, (2 / 3 + 1 + 1) / 3)
                self.assertAlmostEqual(values['汇总']['G20'].value, (0.05 + 0.15 + 0.4) / 3)
                self.assertEqual(values['门店缺货率']['H3'].value, 2 / 3)
                self.assertEqual(values['门店缺货率']['O3'].value, None)
            with ZipFile(path) as zipfile:
                self.assertFalse(any(n.startswith('xl/externalLinks/') for n in zipfile.namelist()))

    def test_supplement_and_duplicate_sales_are_bound_to_exact_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            refs = []
            for i in range(3):
                path = Path(folder) / f'sales{i}.xlsx'
                book = openpyxl.Workbook()
                book.active.append(('仓库/门店编码', '商品编码', '商品名称', '月销', '可用库存', '在途库存', '补货状态'))
                book.active.append(('A', '00001', '隔离测试', 2, 0, 0 if i < 2 else 1, '正常'))
                book.save(path)
                refs.append(file_source(folder, path))
            basis = {'sales': refs[:2]}
            self.assertEqual([r['sku'] for r in sales_rows(folder, basis, None)], ['00001'])
            with self.assertRaisesRegex(ValueError, '两份底稿不同：A'):
                list(sales_rows(folder, {'sales': refs}, None))
            path_for(folder, refs[0]).write_bytes(b'invalid')
            with self.assertRaisesRegex(ValueError, '摘要已变化'):
                path_for(folder, refs[0])


class DailySourceRules(unittest.TestCase):
    def test_november_template_goals_are_frozen_only_without_an_adopted_monthly_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            day = '2026-11-25'
            data = fixture()
            selected = sources.profile(root, day)
            selected['files'] = {key: {'name': key} for key in sources.SUPPLEMENTS if key != 'weeks'}
            selected['sales'] = [{'name': '隔离原件'}]
            sources.save_profile(root, selected)
            wh_refs = {wh: {'account': wh} for wh in data['catalogs']}
            with patch.object(sources, 'resolved_roster', return_value=(data['stores'],None)), \
                    patch.object(sources, 'catalog_refs', return_value=wh_refs):
                basis = sources.freeze(root, day)
                targets = basis['week_defaults']['metrics']
                self.assertEqual([m['target'] for m in targets.values()], [0.10,0.07,0.05])
                self.assertEqual(targets['必采缺货率']['weekly_targets'], ['≤8%','≤7%','≤6%','≤5%'])
                self.assertTrue(all('weekly_values' not in m for m in targets.values()))
                selected['files']['weeks'] = {'name': '当期另行发布目标'}
                sources.save_profile(root, selected)
                self.assertNotIn('week_defaults', sources.freeze(root, day))
            self.assertIsNone(sources.template_weeks('2026-12-01'))

    def test_rule_reuse_respects_effective_day_month_and_explicit_original(self):
        with tempfile.TemporaryDirectory() as folder:
            days = '2026-11-20', '2026-11-23', '2026-11-25', '2026-12-01'
            first = sources.profile(folder, days[0])
            first['files'] = {key: {'effective_day': days[0], 'name': key + '-v1'} for key in sources.RULE_FILES}
            first['files']['must_sell'] = {'name': '该日指标'}
            sources.save_profile(folder, first)
            archived = sources.profile(folder, days[1])
            archived['files']['blacklist'] = {'name': '历史登记原件'}
            sources.save_profile(folder, archived)
            updated = sources.profile(folder, days[2])
            updated['files']['blacklist'] = {'effective_day': days[2], 'name': 'blacklist-v2'}
            sources.save_profile(folder, updated)
            resolve = lambda day: sources.supplement_refs(folder, sources.profile(folder, day))
            self.assertEqual(resolve('2026-11-19'), {})
            self.assertEqual(resolve(days[1])['blacklist']['name'], '历史登记原件')
            self.assertEqual(resolve('2026-11-24')['blacklist']['name'], 'blacklist-v1')
            self.assertEqual(resolve('2026-11-26')['blacklist']['name'], 'blacklist-v2')
            self.assertEqual(set(resolve('2026-11-26')), sources.RULE_FILES)
            self.assertEqual(set(resolve(days[3])), {'stores', 'blacklist'})

    def test_store_scope_reads_saved_routes_for_today_and_published_routes_for_history(self):
        from stockout_repository import Repository

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository(root)
            repo.initialize()
            old_day, new_day = '2026-11-24', '2026-11-25'
            stores = [{'code': '0001', 'name': '同名测试店'}, {'code': '0002', 'name': '同名测试店'}]
            old = repo.save_settings({'stores': stores, 'routes': {
                '0001': {'primary': '河北仓', 'secondary': None},
                '0002': {'primary': '山东仓', 'secondary': None}}})
            run = repo.new_run(day=old_day)
            with repo.connect(write=True) as db:
                publication = db.execute('INSERT INTO publications(day,run,created) VALUES(?,?,?)',
                                         (old_day, run['id'], old_day)).lastrowid
                db.execute('INSERT INTO current_days VALUES(?,?)', (old_day, publication))
            with closing(sqlite3.connect(root/'workbench.sqlite3')) as db, db:
                db.execute('CREATE TABLE stores(code TEXT PRIMARY KEY,data TEXT)')
                db.executemany('INSERT INTO stores VALUES(?,?)', (
                    ('0001', json.dumps({'ui': {'primary': '河北仓', 'secondary': '山东仓'}})),
                    ('0002', json.dumps({'ui': {'primary': '山东仓', 'secondary': None}}))))
            book = openpyxl.Workbook()
            book.active.append(('运营负责人', '主仓', '门店编码', '门店标签'))
            book.active.append(('测试甲', '山东仓', '0001', '河北-新店'))
            book.active.append(('测试乙', '河北仓', '0002', '山东-维稳型'))
            labels = root/'labels.xlsx'
            book.save(labels)
            sources.register_supplement(root, old_day, 'stores', labels, as_rule=True)
            frozen = sources.supplement_refs(root, sources.profile(root, new_day))['stores']
            with patch.object(sources, 'today', return_value=new_day):
                active = sources.store_rules(root, new_day)
                history = sources.store_rules(root, old_day)
                self.assertIsNone(sources.store_rules(root, '2026-11-23'))
            current, proof = sources.resolved_roster(root, frozen, active)
            historical, _ = sources.resolved_roster(root, frozen, history)
            self.assertEqual(proof['version'], old['version'])
            self.assertEqual(current['0001']['secondary'], '山东仓')
            self.assertIsNone(historical['0001']['secondary'])
            self.assertEqual((current['0001']['primary'], current['0002']['primary']), ('河北仓', '山东仓'))
            self.assertEqual((current['0001']['owner'], current['0002']['owner']), ('测试甲', '测试乙'))
            with self.assertRaisesRegex(PendingInputs, '缺门店标签和运营负责人'):
                sources.resolved_roster(root, None, active)
            with self.assertRaisesRegex(PendingInputs, '缺已采用的门店范围'):
                sources.resolved_roster(root, frozen, None)

    def test_adopted_warehouse_original_is_bound_to_day_warehouse_and_content(self):
        from source_archive import capture
        from warehouse_catalog import Repository, revision, REQUIRED

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root/'archive.xlsx'
            book = openpyxl.Workbook()
            book.active.append(('商品编码', '启用', '可用数', '虚拟分类', '产品分类', '供应商'))
            book.active.append(('00001', '启用', 3, '', '食品', '测试供应商'))
            book.active.append(('00002', '备用', 0, '', '', ''))
            book.save(path)
            wh, day = '河北仓', '2026-11-25'
            dbpath = root/'workbench.sqlite3'
            checksum = sources.sha256(path)
            capture(dbpath, path, checksum)
            items = {'00001': dict.fromkeys(REQUIRED) | {'商品编码': '00001'}}
            catalog = {'schema': 1, 'warehouse': wh, 'revision': revision(items), 'items': items,
                       'source_sha256': checksum, 'source_rows': 2, 'kind': 'jst_full_archive',
                       'sheet': book.active.title, 'first_inbound_column': True, 'quarantine_count': 1,
                       'quarantined': [{'商品编码': '00002', 'reason': '测试待核'}],
                       'source_observation': {'warehouse': wh, 'company': '测试河北公司',
                                              'queriedAt': '2026-11-24T23:30:00+00:00'}}
            repo = Repository(root)
            repo.save(catalog)
            payload = {'account': {'warehouse': wh, 'expectedCoid': '01', 'expectedUid': '01'},
                       'receipt': {'status': 'VALIDATED', 'warehouse': wh, 'source_sha256': checksum,
                                   'revision': catalog['revision'], 'rows': 1, 'raw_rows': 2},
                       'ack': {'apply_id': 'adopt1', 'warehouse': wh, 'source_sha256': checksum},
                       'native_task_id': 'original'}
            with closing(sqlite3.connect(dbpath)) as db, db:
                db.execute('''CREATE TABLE catalog_sync_applies(id,batch_id,item_id,warehouse,purpose,state,
                              old_digest,new_digest,source_sha,payload,updated)''')
                db.execute('INSERT INTO catalog_sync_applies VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           ('adopt1', 'batch1', 'a', wh, 'default_catalog', 'APPLIED', None,
                            repo.current_digest(wh), checksum, json.dumps(payload), day))
            preferred = sources.catalog_refs(root, day, sources.profile(root, day))
            ref = preferred[wh]
            self.assertIsNone(preferred['山东仓'])
            self.assertEqual(ref['observed'][:10], day)
            self.assertEqual(list(sources.load_catalog(root, wh, ref, None)), ['00001'])
            self.assertEqual(sources.adopted_catalogs(root, '2026-11-24', (wh,)), {})
            with self.assertRaisesRegex(ValueError, '仓别不符'):
                sources.load_catalog(root, '山东仓', ref, None)
            payload['account']['warehouse'] = '山东仓'
            with closing(sqlite3.connect(dbpath)) as db, db:
                db.execute('UPDATE catalog_sync_applies SET payload=?', (json.dumps(payload),))
            self.assertEqual(sources.adopted_catalogs(root, day, (wh,)), {})
            path.unlink()
            self.assertIn('00001', sources.load_catalog(root, wh, ref, None))
            with closing(sqlite3.connect(dbpath)) as db, db:
                db.execute('UPDATE source_files SET content=?', (b'invalid',))
            with self.assertRaisesRegex(ValueError, '原文件快照校验失败'):
                sources.load_catalog(root, wh, ref, None)
