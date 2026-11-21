"""Warehouse tag membership through routing, budget and persisted signing."""
import copy
import sys
import unittest
from contextlib import closing
from tempfile import TemporaryDirectory
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import batch_service
import engine
import normal_orders
import order_analytics
import priority_list
import warehouse_catalog
import warehouse_execution
import work_repository
from result_semantics import order_quantity
from table_records import bridge_table
from version_info import MODEL_VERSION

SD, HB = '山东仓', '河北仓'
SKUS = ('0011', '0012')


def snapshots():
    catalogs = {}
    for warehouse in (SD, HB):
        rows = {}
        for index, sku in enumerate(SKUS):
            rows[sku] = {'商品编码': sku, '商品名称': '测试饮品' + sku, '分类': '饮品',
                         '基本售价': 3, '出库箱规': 6, '商品状态': '启用', '可用数': 100,
                         '商品标签': '新品，必卖清单' if warehouse == HB else '非必卖清单', '_row': index + 2}
        catalogs[warehouse] = {'schema': 1, 'warehouse': warehouse, 'items': rows,
                               'revision': warehouse_catalog.revision(rows), 'imported_at': '2026-11-21T10:00:00',
                               'source_path': warehouse + '.xlsx', 'sheet': '商品资料', 'source_sha256': 'qa'}
    return catalogs


def calculate(catalogs, warehouse, profile, *, store='QA', secondary=None, month=60, budget=None):
    sales = [{'仓库/门店编码': store, '仓库/门店名称': '测试门店' + store, '商品编码': sku,
              '商品名称': '测试饮品' + sku, '日销': month/30, '周销': month*7/30, '月销': month,
              '可用库存': 0, '在途库存': 0, '缺货天数（天）': 0, '补货状态': '正常'} for sku in SKUS]
    loaded = warehouse_catalog.prepare_records(bridge_table(list(sales[0]), sales, '补货建议'), catalogs)
    settings = {'warehouses': engine.DEFAULT_WAREHOUSES, 'exception': False, 'budget': budget,
                'supply_route': {'primary': warehouse, 'secondary': secondary}}
    return batch_service.calculate_store(loaded, store, settings, profile)


def save_catalog_revision(catalog):
    catalog['revision'] = warehouse_catalog.revision(catalog['items'])


class WarehouseMustSell(unittest.TestCase):
    def test_same_sku_follows_actual_warehouse_and_drives_existing_minimum(self):
        catalogs = snapshots()
        profile = priority_list.catalog_profile(catalogs)
        for warehouse, expected in ((SD, False), (HB, True)):
            with self.subTest(warehouse=warehouse):
                result = calculate(catalogs, warehouse, profile, month=0, budget=36)
                self.assertEqual([r['E'] for r in result['rows']], list(SKUS))
                self.assertTrue(all(r['AU'] == warehouse and r['_must_sell'] is expected for r in result['rows']))
                self.assertEqual(sum(order_quantity(r) for r in result['rows']), 12 if expected else 0)
                self.assertEqual(result['must_list']['total'], 2 if expected else 0)
                self.assertEqual(set(result['must_list']['warehouses']), {warehouse})

    def test_routing_to_secondary_uses_secondary_tag_and_respects_disabled(self):
        catalogs = snapshots()
        catalogs[SD]['items'][SKUS[0]]['可用数'] = 0
        catalogs[HB]['items'][SKUS[0]]['商品状态'] = '禁用'
        save_catalog_revision(catalogs[SD])
        save_catalog_revision(catalogs[HB])
        result = calculate(catalogs, SD, priority_list.catalog_profile(catalogs), secondary=HB)
        first, second = result['rows']
        self.assertEqual((first['AU'], first['_must_sell'], order_quantity(first)), (HB, True, 0))
        self.assertEqual((second['AU'], second['_must_sell']), (SD, False))
        self.assertGreater(order_quantity(second), 0)
        audit = {(r['warehouse'], r['sku']): r['state'] for r in result['must_list_audit']}
        self.assertIn('禁用', audit[(HB, SKUS[0])])
        self.assertEqual(audit[(HB, SKUS[1])], '本次归属山东仓')

    def test_empty_tag_list_is_known_and_missing_or_stale_evidence_is_not_used(self):
        catalogs = snapshots()
        profile = priority_list.catalog_profile(catalogs)
        self.assertEqual(profile['warehouses'][SD]['count'], 0)
        self.assertFalse(calculate(catalogs, SD, profile)['rows'][0]['_must_sell'])
        del catalogs[SD]['items'][SKUS[0]]['商品标签']
        save_catalog_revision(catalogs[SD])
        with self.assertRaisesRegex(ValueError, '与实际供货资料版本不同'):
            calculate(catalogs, SD, profile)
        profile = priority_list.catalog_profile(catalogs)
        with self.assertRaisesRegex(ValueError, '山东仓.*必卖标签待核'):
            calculate(catalogs, SD, profile)
        self.assertTrue(calculate(catalogs, HB, profile)['rows'][0]['_must_sell'])

    def test_signed_legacy_and_tagged_results_keep_their_original_scope(self):
        catalogs = snapshots()
        legacy = priority_list.make_profile([{'sku': SKUS[0], 'name': '旧名单'}], '旧导入清单')
        first = calculate(catalogs, SD, legacy, store='OLD')
        profile = priority_list.catalog_profile(catalogs)
        second = calculate(catalogs, HB, profile, store='NEW', secondary=SD)
        with TemporaryDirectory() as folder:
            work = work_repository.Repository(folder)
            repo = normal_orders.Repository(work)
            results = {'OLD': first, 'NEW': second}
            payload = {'schema': 1, 'day': work_repository.today(), 'model_version': MODEL_VERSION,
                       'input_dirty': False, 'results': results,
                       'states': {store: {'name': r['store_name'], 'state': '已完成'} for store, r in results.items()}}
            repo.confirm(payload, list(results), None, {}, headers_only=True)
            with closing(work.connect()) as db:
                heads = {head['store']: head for head in warehouse_execution.heads(db)}
            signed = {store: warehouse_execution.get_order(repo, head) for store, head in heads.items()}
            for store, result in results.items():
                saved = signed[store]['payload']['result']
                self.assertEqual(saved['must_list']['revision'], result['must_list']['revision'])
                self.assertEqual([r['_must_sell'] for r in saved['rows']], [r['_must_sell'] for r in result['rows']])
            self.assertEqual(set(signed['NEW']['payload']['result']['must_list']['warehouses']), {HB})

            changed = copy.deepcopy(catalogs)
            changed[HB]['items'][SKUS[0]]['商品标签'] = ''
            save_catalog_revision(changed[HB])
            current = priority_list.catalog_profile(changed)
            self.assertFalse(calculate(changed, HB, current)['rows'][0]['_must_sell'])
            for store, head in heads.items():
                archive = warehouse_execution.get_order(repo, head)
                self.assertEqual(archive, signed[store])
                row = archive['payload']['result']['rows'][0]
                self.assertEqual(order_analytics.membership(archive['payload']['result'], row, SKUS[0], head['warehouse']), '必采')

    def test_archive_fallback_uses_frozen_warehouse_when_row_is_absent(self):
        catalogs = snapshots()
        profile = priority_list.catalog_profile(catalogs)
        result = calculate(catalogs, SD, profile, secondary=HB)
        self.assertEqual(order_analytics.membership(result, None, SKUS[0], SD), '非必采')
        self.assertEqual(order_analytics.membership(result, None, SKUS[0], HB), '必采')
        self.assertEqual(order_analytics.membership(result, None, SKUS[0]), '属性待核')
    def test_one_sku_tagged_in_both_warehouses_has_independent_entries(self):
        catalogs = snapshots()
        catalogs[SD]['items'][SKUS[0]]['商品标签'] = '必卖清单'
        save_catalog_revision(catalogs[SD])
        profile = priority_list.catalog_profile(catalogs)
        result = calculate(catalogs, SD, profile, secondary=HB)
        self.assertTrue(result['rows'][0]['_must_sell'])
        self.assertEqual(result['must_list']['total'], 3)
        self.assertEqual(result['must_list']['matched'], 1)
        self.assertEqual({p['warehouse'] for p in result['must_list_audit'] if p['sku'] == SKUS[0]}, {SD, HB})

    def test_removing_one_warehouse_preserves_other_frozen_tag_evidence(self):
        import data_maintenance as maintenance

        catalogs = snapshots()
        catalogs[SD]['items'][SKUS[0]].update({'商品标签': '必卖清单', '可用数': 0})
        save_catalog_revision(catalogs[SD])
        profile = priority_list.catalog_profile(catalogs)
        result = calculate(catalogs, SD, profile, secondary=HB)
        self.assertEqual([r['AU'] for r in result['rows']], [HB, SD])
        day = work_repository.today()
        payload = {'schema': 1, 'day': day, 'model_version': MODEL_VERSION, 'input_dirty': False,
                   'results': {'QA': result}, 'states': {'QA': {'name': result['store_name'], 'state': '已完成'}}}
        with TemporaryDirectory() as folder:
            work = work_repository.Repository(folder)
            repo = normal_orders.Repository(work)
            repo.confirm(payload, ['QA'], None, {}, headers_only=True)
            with closing(work.connect()) as db:
                heads = {head['warehouse']: head for head in warehouse_execution.heads(db)}
            before = warehouse_execution.get_order(repo, heads[SD])
            projected = maintenance.strip_current_warehouses(payload, [heads[HB]])
            maintenance.execute(work.path, maintenance.prepare(work.path, [heads[HB]]), True)
            saved = repo.get(day, 'QA')['payload']['result']
            self.assertEqual(warehouse_execution.get_order(repo, heads[SD]), before)
            for remaining in (projected['results']['QA'], saved):
                self.assertEqual([r['AU'] for r in remaining['rows']], [SD])
                self.assertEqual(remaining['must_list']['total'], 1)
                self.assertEqual([(r['warehouse'], r['sku']) for r in remaining['must_list_audit']], [(SD, SKUS[0])])
                self.assertEqual(order_analytics.membership(remaining, None, SKUS[0], SD), '必采')

    def test_existing_schedule_seal_matches_while_task_uses_new_tag_source(self):
        import scheduled_execution as execution
        with TemporaryDirectory() as folder:
            previous = priority_list.make_profile([{'sku': SKUS[0]}], '原设置')
            priority_list.Repository(folder).save(previous)
            frozen = {'profile': priority_list.catalog_profile({}), 'jobs': {}}
            old_signature = execution.check_signature({**frozen, 'profile': previous})
            self.assertTrue(execution.matches_check_signature(frozen, old_signature, folder))
            self.assertFalse(execution.matches_check_signature({**frozen, 'workers': 4}, old_signature, folder))
            self.assertTrue(priority_list.is_catalog_profile(frozen['profile']))


if __name__ == '__main__':
    unittest.main()
