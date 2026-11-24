"""Batch plan import, round-trip and UI entry using isolated data."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import import_templates
import parameter_presets as presets
import parameter_schema
import replenishment_plan_tables as tables
import replenishment_plans as plans
import work_repository as wr


class PlanTables(TestCase):
    def setUp(self):
        sandbox = TemporaryDirectory()
        self.addCleanup(sandbox.cleanup)
        self.root = Path(sandbox.name)
        self.work = wr.Repository(self.root / 'settings')
        self.repo = plans.Repository(self.work.path)
        self.names = {'0001': '一店', '0002': '二店', '0003': '三店'}

    def book(self, rows, name='计划导入.xlsx'):
        book = import_templates.make_workbook(tables.KIND, blank_rows=0)
        for row in rows:
            book[tables.SHEET].append(row)
        return import_templates.save_workbook(book, self.root / name)

    def review(self, path):
        data = tables.read([path])
        snapshot = presets.snapshot(self.work)
        return data, tables.prepare(data, snapshot, self.names), snapshot

    def event_count(self):
        with closing(self.work.connect()) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='replenishment_plan_events'").fetchone():
                return db.execute('SELECT count(*) FROM replenishment_plan_events').fetchone()[0]
            return 0

    def test_batch_round_trip_preserves_store_order_and_parameter_choice(self):
        config = parameter_schema.standard()
        config['warehouses']['河北仓'] = [5.5, 2.75]
        preset = self.work.save_preset('河北常规', config)
        file = self.book([
            ['北线', '0001', '一店', 20, '河北常规', '首轮'],
            ['南线', '0003'],
            ['北线', '0002', None, 10],
        ])
        data, review, snapshot = self.review(file)
        self.assertEqual(len(review['edits']), 2)
        self.assertEqual(self.repo.list(), [])
        north, south = tables.save(self.work.path, data, review, snapshot)
        self.assertEqual((north['name'], south['name']), ('北线', '南线'))
        self.assertEqual([item['code'] for item in north['stores']], ['0002', '0001'])
        self.assertIsNone(north['stores'][0]['preset_id'])
        self.assertEqual(north['stores'][1]['preset_id'], preset['id'])
        self.assertEqual(presets.freeze(north, self.work)['_resolved_presets']['0001']['config'], wr.loads(wr.dumps(config)))
        self.assertEqual((self.work.stores(), self.work.list_jobs()), ({}, []))
        self.assertEqual(self.event_count(), 2)
        exported = tables.export(self.root / '计划回导.xlsx', presets.snapshot(self.work))
        _, recheck, _ = self.review(exported)
        self.assertEqual((recheck['unchanged'], recheck['edits']), (2, []))

    def test_update_preview_keeps_prior_preset_and_shows_stores_removed(self):
        preset = self.work.save_preset('已有选择', parameter_schema.standard())
        original = self.repo.save({
            'name': '北线', 'parameter_mode': presets.MODE, 'config': parameter_schema.standard(), 'note': '保留备注',
            'stores': [{'code': '0001', 'name': '一店', 'preset_id': preset['id']},
                       {'code': '0002', 'name': '二店', 'preset_id': None}],
        })
        data, review, snapshot = self.review(self.book([['北线', '0001'], ['北线', '0003']]))
        self.assertTrue(any(row[1] == '移出' and '0002' in row[4] for row in review['rows']))
        updated, = tables.save(self.work.path, data, review, snapshot)
        self.assertEqual((updated['id'], updated['revision'], updated['note']), (original['id'], 2, '保留备注'))
        self.assertEqual([row.get('preset_id') for row in updated['stores']], [preset['id'], None])
        self.assertEqual([row['code'] for row in updated['stores']], ['0001', '0003'])

    def test_legacy_plan_keeps_original_parameter_mode_when_roster_changes(self):
        config = parameter_schema.standard()
        config['warehouses']['山东仓'] = [6, 2]
        old = self.repo.save({'name': '旧计划', 'config': config, 'parameter_fields': [['warehouses', '山东仓', 0]],
                              'stores': [{'code': '0001', 'name': '一店'}]})
        data, review, snapshot = self.review(self.book([['旧计划', '0002']]))
        updated, = tables.save(self.work.path, data, review, snapshot)
        self.assertEqual(updated['config'], old['config'])
        self.assertEqual(updated['parameter_fields'], old['parameter_fields'])
        self.assertNotIn('parameter_mode', updated)
        explicit = self.book([['旧计划', '0001', None, None, tables.FOLLOW]], '转换.xlsx')
        with self.assertRaisesRegex(ValueError, '旧参数模式'):
            self.review(explicit)

    def test_ambiguous_and_invalid_rows_cannot_create_other_plans(self):
        valid = ['有效计划', '0001']
        cases = [
            ([['重复', '0002'], ['重复', '0002']], '门店编码重复'),
            ([['冲突', '0002', None, 1], ['冲突', '0003', None, 1]], '不同的正整数'),
            ([['冲突', '0002', '疑似另一店']], '门店名称与登记不符'),
            ([['冲突', '不存在的门店']], '未在门店目录登记'),
            ([['冲突', '0002', None, None, '不存在的预案']], '未找到或已经停用'),
        ]
        for index, (rows, error) in enumerate(cases):
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                self.review(self.book([valid, *rows], f'校验-{index}.xlsx'))
        self.assertEqual(self.repo.list(), [])

    def test_concurrent_edit_or_changed_source_blocks_entire_batch(self):
        source = self.book([['北线', '0001'], ['南线', '0002']])
        data, review, snapshot = self.review(source)
        self.repo.save({'name': '北线', 'config': parameter_schema.standard(), 'stores': [{'code': '0003', 'name': '三店'}]})
        with self.assertRaisesRegex(wr.Conflict, '已变化'):
            tables.save(self.work.path, data, review, snapshot)
        data, review, snapshot = self.review(source)
        with Path(source).open('ab') as stream:
            stream.write(b'changed after review')
        with self.assertRaisesRegex(ValueError, '预览后发生变化'):
            tables.save(self.work.path, data, review, snapshot)
        self.assertEqual(len(self.repo.list()), 1)
        self.assertEqual(self.event_count(), 1)

    def test_database_failure_rolls_back_plans_and_history_together(self):
        data, review, snapshot = self.review(self.book([['北线', '0001'], ['南线', '0002']]))
        real_event = self.repo.event

        def fail_second(db, action, before, after):
            if after['name'] == '南线':
                raise OSError('合成写入失败')
            real_event(db, action, before, after)

        with patch.object(plans.Repository, 'event', staticmethod(fail_second)):
            with self.assertRaisesRegex(OSError, '合成写入失败'):
                tables.save(self.work.path, data, review, snapshot)
        self.assertEqual(self.repo.list(), [])
        self.assertEqual(self.event_count(), 0)

    def test_plan_library_import_button_honors_preview_confirmation(self):
        from PySide6.QtWidgets import QApplication, QDialog, QPushButton
        from replenishment_plan_ui import Library

        app = QApplication.instance() or QApplication([])
        host = QDialog()
        host.work_repository = self.work
        host.loaded = None
        host.closing = False
        dialog = Library(host)
        button, = (b for b in dialog.findChildren(QPushButton) if b.text() == '批量导入补货计划')
        file = self.book([['北线', '0001', '一店'], ['南线', '0002', '二店']])
        with patch('import_ui.choose', return_value=[file]), patch('replenishment_plan_ui.review_rows', side_effect=[False, True]), \
             patch('replenishment_plan_ui.QMessageBox.information') as messages, patch('replenishment_plan_ui.QMessageBox.warning') as warnings:
            button.click()
            self.assertEqual(self.repo.list(), [])
            button.click()
            self.assertEqual(dialog.table.rowCount(), 2)
            self.assertEqual({p['name'] for p in self.repo.list()}, {'北线', '南线'})
            messages.assert_called_once()
            warnings.assert_not_called()
        dialog.close()
        host.close()
        app.processEvents()
