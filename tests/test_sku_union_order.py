"""Exercise actual lifecycle/analytics projections against the frozen release.

All facts are synthetic and SQLite stays in memory. The optional release source
is loaded without a bytecode cache; no application settings or platforms open.
"""
import copy
import os
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src')]
import order_lifecycle as lifecycle
import order_analytics as analytics
import operations_flow
import work_repository as wr
from runtime_support import Cancelled


DEFAULT_BASELINE = Path(
    r'D:\优团工作文件夹\outputs\replenishment_mvp_20261029\build_0.52.5_rc1\source')


def typed(value):
    """Compare order and scalar types, including False versus numeric zero."""
    if isinstance(value, dict):
        return ('dict', tuple((typed(k), typed(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return (type(value).__name__, tuple(typed(v) for v in value))
    return (type(value).__name__, repr(value))


def frozen_module(name, path):
    module = types.ModuleType(name)
    module.__file__ = str(path)
    exec(compile(path.read_text(encoding='utf-8-sig'), str(path), 'exec'), module.__dict__)
    return module


def signed_row(sku, quantity=4, **extra):
    return dict(E=sku, D='合成商品 ' + str(sku), A='合成门店', B='S',
                AU='山东仓', _source='资料匹配', S='启用', AS=quantity, BV=8,
                V=2, G='瓶', U='快销', J=3, K=0, L=0, N=1,
                _meta={'blocking': [], 'export_status': '正常'}, **extra)


def document_row(sku, quantity='3', **extra):
    return dict(sku=sku, name='环节商品 ' + str(sku), base_quantity=quantity,
                amount=None, currency='', processing_note='', **extra)


def make_union_fixture(signature, documents=None, *, current_route=False,
                       document_schema=2, legacy_values=None):
    """Reusable small synthetic fixture; caller owns and closes the connection."""
    db = sqlite3.connect(':memory:')
    lifecycle.schema(db)
    db.execute('CREATE TABLE normal_work_revisions(revision TEXT,payload BLOB)')
    head = dict(oid='union-order', revision=1, warehouse='山东仓', store='S',
                day='2026-11-13', number='BH-union-SD', digest='a' * 64)
    order = {**head, 'current_revision': 1,
             'payload': {'result': {'store': 'S', 'store_name': '合成门店',
                                    'rows': copy.deepcopy(signature)}}}
    if current_route:
        operations_flow.schema(db)
        db.execute('INSERT INTO lifecycle_routes VALUES(?,?,2)',
                   (head['oid'], head['revision']))
    tokens = {}
    route = operations_flow.CURRENT if current_route else operations_flow.LEGACY
    for stage in route:
        rows = (documents or {}).get(stage)
        if rows is None:
            tokens[stage] = None
            continue
        payload = dict(schema=document_schema, rows=copy.deepcopy(rows), files=[],
                       pending=False, basis={'dependencies': copy.deepcopy(tokens)})
        digest = lifecycle.fingerprint(payload)
        db.execute('INSERT INTO lifecycle_versions VALUES(?,?,?,?,?,?,?,?)',
                   (*lifecycle.key(head), stage, 1, '2026-11-13T10:00:00+08:00',
                    digest, wr.dumps(payload)))
        tokens[stage] = {'version': 1, 'digest': digest}
    if legacy_values is not None:
        db.execute('CREATE TABLE merchant_feedback(oid TEXT,revision INTEGER,version INTEGER,saved TEXT,payload TEXT)')
        db.execute('INSERT INTO merchant_feedback VALUES(?,?,?,?,?)',
                   (head['oid'], head['revision'], 1, '2026-11-13T09:00:00+08:00',
                    wr.dumps({'values': legacy_values})))
    db.commit()
    return db, order, head


class SkuUnionOrder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        baseline = Path(os.environ.get('SKU_UNION_BASELINE', DEFAULT_BASELINE))
        cls.old_lifecycle = cls.old_analytics = cls.old_io = None
        if (baseline / 'src/order_lifecycle.py').is_file():
            cls.old_lifecycle = frozen_module('frozen_union_lifecycle', baseline / 'src/order_lifecycle.py')
            cls.old_analytics = frozen_module('frozen_union_analytics', baseline / 'src/order_analytics.py')
            cls.old_analytics.lifecycle = cls.old_lifecycle
            cls.old_io = frozen_module('frozen_union_io', baseline / 'src/lifecycle_io.py')

    def setUp(self):
        self.connections = []

    def tearDown(self):
        for db in self.connections:
            db.close()

    def fixture(self, signature, documents=None, **options):
        result = make_union_fixture(signature, documents, **options)
        self.connections.append(result[0])
        return result

    def snapshot(self, db, order, head):
        before = tuple(db.iterdump()), typed(order), db.total_changes
        actual = lifecycle.snapshot(None, head, db=db, order=order)
        if self.old_lifecycle:
            expected = self.old_lifecycle.snapshot(None, head, db=db, order=order)
            self.assertEqual(typed(actual), typed(expected))
        self.assertEqual((tuple(db.iterdump()), typed(order), db.total_changes), before)
        return actual

    def facts(self, db, order, head, view):
        before = tuple(db.iterdump()), typed(order), typed(view), db.total_changes
        actual = analytics.build_facts(order, head, view, analytics.Evidence(db))
        if self.old_analytics:
            expected = self.old_analytics.build_facts(order, head, view, self.old_analytics.Evidence(db))
            self.assertEqual(typed(actual), typed(expected))
        self.assertEqual((tuple(db.iterdump()), typed(order), typed(view), db.total_changes), before)
        return actual

    def test_release_comparison_uses_frozen_actual_functions(self):
        if not self.old_lifecycle:
            self.skipTest('optional frozen 0.52.5 source is not available')
        self.assertNotEqual(lifecycle.snapshot.__code__.co_filename,
                            self.old_lifecycle.snapshot.__code__.co_filename)
        self.assertNotEqual(analytics.build_facts.__code__.co_filename,
                            self.old_analytics.build_facts.__code__.co_filename)

    def test_legacy_route_cross_stage_extras_keep_first_appearance_order(self):
        db, order, head = self.fixture(
            [signed_row('0002'), signed_row('0001'), signed_row('中文')],
            {'return': [document_row('RET'), document_row('0001'), document_row('RET')],
             'payment': [document_row('PAY'), document_row('RET'), document_row('0002')],
             'receipt': [document_row('END'), document_row('PAY'), document_row('END')]})
        view = self.snapshot(db, order, head)
        expected = ['0002', '0001', '中文', 'RET', 'PAY', 'END']
        self.assertEqual([r[0] for r in view['rows']], expected)
        self.assertEqual(view['flow_version'], 1)
        facts = self.facts(db, order, head, view)
        self.assertEqual([r['sku'] for r in facts], expected)
        self.assertEqual(analytics.aggregate(facts)['rows'], len(expected))

    def test_current_route_operations_only_extra_has_original_two_views_order(self):
        db, order, head = self.fixture(
            [signed_row('签发'), signed_row('0001')],
            {'return': [document_row('回传外'), document_row('0001')],
             'operations': [document_row('运营外'), document_row('回传外')],
             'payment': [document_row('付款外'), document_row('运营外')],
             'receipt': [document_row('实收外'), document_row('付款外')]}, current_route=True)
        view = self.snapshot(db, order, head)
        self.assertEqual(view['flow_version'], 2)
        self.assertEqual([r[0] for r in view['rows']],
                         ['签发', '0001', '回传外', '付款外', '运营外', '实收外'])
        facts = self.facts(db, order, head, view)
        self.assertEqual([r['sku'] for r in facts],
                         ['签发', '0001', '回传外', '运营外', '付款外', '实收外'])
        actual_flow = lifecycle.flow_rows(view)
        if self.old_lifecycle:
            self.assertEqual(typed(actual_flow), typed(self.old_lifecycle.flow_rows(view)))
        self.assertEqual(len(actual_flow), len(view['rows']))

    def test_only_operations_addition_is_appended_by_existing_flow_rows(self):
        db, order, head = self.fixture([signed_row('0001')],
            {'operations': [document_row('仅运营')]}, current_route=True)
        view = self.snapshot(db, order, head)
        self.assertEqual([r[0] for r in view['rows']], ['0001'])
        self.assertEqual([r[0] for r in lifecycle.flow_rows(view)], ['0001', '仅运营'])
        self.assertEqual([r['sku'] for r in self.facts(db, order, head, view)], ['0001', '仅运营'])

    def test_legacy_feedback_missing_lines_and_zero_quantity_keep_old_scope(self):
        for values in ({'0001': 0}, {'0001': 2, '中文': 0}):
            with self.subTest(values=values):
                db, order, head = self.fixture([signed_row('0001'), signed_row('中文')],
                    {'payment': [document_row('付款外', None)]}, legacy_values=values)
                view = self.snapshot(db, order, head)
                self.assertEqual([r[0] for r in view['rows']], ['0001', '中文', '付款外'])
                facts = self.facts(db, order, head, view)
                self.assertEqual(len(facts), 3)
                self.assertEqual(facts[0]['quantities']['return'], analytics.num(values['0001']))
                self.assertIsNone(facts[-1]['quantities']['payment'])

    def test_current_and_legacy_document_schema_zero_and_missing_remain_distinct(self):
        for schema in (1, 2):
            with self.subTest(schema=schema):
                db, order, head = self.fixture([signed_row('0001'), signed_row('缺行')],
                    {'return': [document_row('0001', '0'), document_row('空数量', None)]},
                    document_schema=schema)
                view = self.snapshot(db, order, head)
                facts = self.facts(db, order, head, view)
                self.assertEqual([r['sku'] for r in facts], ['0001', '缺行', '空数量'])
                self.assertEqual(facts[0]['quantities']['return'], analytics.num(0))
                self.assertEqual(facts[1]['quantities']['return'], analytics.num(0) if schema == 2 else None)
                self.assertIsNone(facts[2]['quantities']['return'])

    def test_hashable_boundary_keys_preserve_python_list_membership_and_types(self):
        for first, equal in ((0, False), (False, 0)):
            with self.subTest(first=first):
                db, order, head = self.fixture(
                    [signed_row(first), signed_row('0'), signed_row(None),
                     signed_row('None'), signed_row('0001'), signed_row('中文')],
                    {'return': [document_row(equal), document_row(None), document_row('新增')]})
                view = self.snapshot(db, order, head)
                expected = [first, '0', None, 'None', '0001', '中文', '新增']
                self.assertEqual(typed([r[0] for r in view['rows']]), typed(expected))
                self.assertEqual(typed([r['sku'] for r in self.facts(db, order, head, view)]), typed(expected))

    def test_initial_duplicate_signature_semantics_are_not_changed(self):
        db, order, head = self.fixture([signed_row('重复'), signed_row('0001'), signed_row('重复', 9)],
                                      {'return': [document_row('清单外')]})
        view = self.snapshot(db, order, head)
        self.assertEqual([r[0] for r in view['rows']], ['重复', '0001', '清单外'])
        self.assertEqual(view['rows'][0][2], 9)
        for module in (analytics, self.old_analytics):
            if module:
                with self.assertRaisesRegex(ValueError, '商品编码重复'):
                    module.build_facts(order, head, view, module.Evidence(db))

    def test_empty_signature_and_empty_document_keep_exact_row_counts(self):
        for documents, expected in (({}, []), ({'return': []}, []),
                                    ({'return': [document_row('0001')]}, ['0001'])):
            with self.subTest(documents=documents):
                db, order, head = self.fixture([], documents)
                view = self.snapshot(db, order, head)
                self.assertEqual([r[0] for r in view['rows']], expected)
                facts = self.facts(db, order, head, view)
                self.assertEqual([r['sku'] for r in facts], expected)
                self.assertEqual(analytics.aggregate(facts)['rows'], len(expected))

    def test_bad_document_digest_and_unhashable_sku_are_still_rejected(self):
        db, order, head = self.fixture([signed_row('0001')], {'return': [document_row('新增')]})
        db.execute("UPDATE lifecycle_versions SET digest='corrupted'")
        for module in (lifecycle, self.old_lifecycle):
            if module:
                with self.assertRaisesRegex(ValueError, '快照校验失败'):
                    module.snapshot(None, head, db=db, order=order)
        db, order, head = self.fixture([signed_row(['非法列表'])])
        for module in (lifecycle, self.old_lifecycle):
            if module:
                with self.assertRaises(TypeError):
                    module.snapshot(None, head, db=db, order=order)

    def test_projection_cancellation_still_raises_without_writes(self):
        db, order, head = self.fixture([signed_row('0001')], {'return': [document_row('新增')]})
        view = self.snapshot(db, order, head)
        event = threading.Event(); event.set()
        before = tuple(db.iterdump())
        for module in (lifecycle, self.old_lifecycle):
            if module:
                with self.assertRaises(Cancelled):
                    module.snapshot(None, head, db=db, order=order, cancel=event)
        for module in (analytics, self.old_analytics):
            if module:
                with self.assertRaises(Cancelled):
                    module.build_facts(order, head, view, module.Evidence(db), event)
        self.assertEqual(tuple(db.iterdump()), before)

    def test_file_sku_validation_remains_strict_before_projection(self):
        import openpyxl
        import lifecycle_io
        parent = os.environ.get('SKU_UNION_QA_ROOT')
        with tempfile.TemporaryDirectory(prefix='sku-union-', dir=parent) as temporary:
            cases = [(None, False), ('', False), (False, False), (1.5, False),
                     (0, True), (-1, True), ('0001', True), ('中文', True)]
            for index, (sku, valid) in enumerate(cases):
                with self.subTest(sku=sku):
                    target = Path(temporary) / ('invalid-' + str(index) + '.xlsx')
                    book = openpyxl.Workbook(); ws = book.active
                    headers = lifecycle_io.fields('return'); ws.append(headers)
                    values = {'补货单号': 'BH-union-SD', '商品编码': sku, '门店确认数量': 1,
                              '数量单位': '库存单位', '门店编码': 'S', '供货仓': '山东仓'}
                    ws.append([values.get(h) for h in headers]); book.save(target); book.close()
                    file = lifecycle_io.inspect([target])[0]; sheet = file['sheets'][0]
                    spec = {**file, **sheet, 'mapping': {h: n for n, h in enumerate(sheet['headers'])}}
                    if valid:
                        actual = lifecycle_io.scan('return', [spec])
                        if self.old_io:
                            self.assertEqual(typed(actual), typed(self.old_io.scan('return', [spec])))
                        self.assertEqual(actual['rows'][0]['sku'], str(sku))
                    else:
                        for module in (lifecycle_io, self.old_io):
                            if module:
                                with self.assertRaises(ValueError): module.scan('return', [spec])


if __name__ == '__main__':
    unittest.main()
