from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import order_migration
import order_selection
import order_merge_migration
from merge_fixture import Order, file_hashes, second_order, shared_purchase


class OrderSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = Order(self.root / 'source', code='FIRST')
        self.other = second_order(self.source, self.source.code, oid='synthetic-oid-SHARED', day='2026-11-15')
        self.outside = second_order(self.source, 'OUTSIDE', day='2026-11-16')
        shared_purchase(self.source, [self.source.h, self.other])
        self.target = Order(self.root / 'target', code='LOCAL')

    def test_complete_cross_day_scope_is_readonly_and_imports_once(self):
        before = file_hashes(self.source.root)
        scope = order_selection.preview(self.source.root, (self.source.oid,), '')
        self.assertEqual([row['oid'] for row in scope['orders']], [self.source.oid, self.other['oid']])
        self.assertEqual([row['requested'] for row in scope['orders']], [True, False])
        self.assertEqual(set(scope['orders'][0]['warehouses']), {'山东仓', '河北仓'})
        target = self.root / 'selection.zip'
        result = order_selection.export(self.source.root, target, scope)
        self.assertEqual(file_hashes(self.source.root), before)
        self.assertEqual(result['manifest']['format'], order_migration.SELECTION_FORMAT)
        self.assertEqual(result['manifest']['counts']['normal_orders'], 2)
        with self.assertRaisesRegex(ValueError, '请使用「合并导入订单」'):
            order_migration.prepare(self.target.root, target)
        plan = order_merge_migration.prepare(self.target.root, target)
        self.assertEqual(plan['merge_report']['counts']['added'], 2)
        with self.assertRaisesRegex(ValueError, '只能合并导入'):
            order_migration.apply({**plan, 'mode': 'replace'})
        with patch('order_merge_migration._check_other_processes'):
            order_migration.apply(plan)
        repeat = order_merge_migration.prepare(self.target.root, target)
        self.assertEqual(repeat['merge_report']['counts'], {'added': 0, 'duplicate': 2, 'conflict': 0, 'deleted': 0})
        with closing(sqlite3.connect(self.target.path)) as db:
            identities = {oid for oid, in db.execute('SELECT oid FROM normal_orders')}
            self.assertEqual(identities, {self.target.oid, self.source.oid, self.other['oid']})
            self.assertEqual(db.execute('SELECT count(*) FROM purchase_documents').fetchone()[0], 1)

    def test_business_day_expands_and_stale_confirmation_is_rejected(self):
        scope = order_selection.preview(self.source.root, (), self.source.day)
        self.assertEqual(scope['requested'], [self.source.oid])
        self.assertEqual(set(scope['business']), {self.source.oid, self.other['oid']})
        self.source.save('return', 7, warehouse='山东仓')
        target = self.root / 'stale.zip'
        with self.assertRaisesRegex(ValueError, '已变化'):
            order_selection.export(self.source.root, target, scope)
        self.assertFalse(target.exists())

    def test_package_manifest_must_match_contents(self):
        scope = order_selection.preview(self.source.root, (), self.source.day)
        target = self.root / 'valid.zip'
        order_selection.export(self.source.root, target, scope)
        import json
        invalid = self.root / 'wrong-identity.zip'
        with zipfile.ZipFile(target) as source, zipfile.ZipFile(invalid, 'x') as destination:
            for name in source.namelist():
                contents = source.read(name)
                if name == 'manifest.json':
                    manifest = json.loads(contents)
                    manifest['selection']['included'] = [self.source.oid, self.outside['oid']]
                    contents = json.dumps(manifest, ensure_ascii=False).encode('utf-8')
                destination.writestr(name, contents)
        with self.assertRaisesRegex(ValueError, '与库内订单不一致'):
            order_merge_migration.prepare(self.target.root, invalid)

    def test_missing_required_original_blocks_export(self):
        with closing(sqlite3.connect(self.source.path)) as db, db:
            db.execute('UPDATE purchase_files SET content=?', (b'damaged attachment',))
        with self.assertRaisesRegex(ValueError, '原件内容与SHA不一致'):
            order_selection.preview(self.source.root, (self.source.oid,), '')
