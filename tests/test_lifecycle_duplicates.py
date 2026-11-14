"""Contradictory source rows require explicit choices; all data is synthetic."""
import copy
import hashlib
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import test_lifecycle as fixture
import lifecycle_batch as batch
import lifecycle_duplicates as duplicates
import order_lifecycle as service
import work_repository as wr


class LifecycleDuplicateTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.LifecycleTests(); self.f.setUp()
        self.addCleanup(self.f.tearDown)

    def document(self, stage='return', items=None):
        items = items or [{'门店确认数量': 0}, {'门店确认数量': 7}]
        document = batch.group_scan(self.f.scan(stage, items))[0]
        document['target'] = self.f.h
        return document

    def choices(self, document, index):
        return {c['token']: duplicates.candidate_id(c['rows'][index]) for c in duplicates.find(document['scan']['rows'])}

    def test_all_conflicts_explain_actual_file_rows_quantities_and_differing_names(self):
        document = self.document(items=[{'门店确认数量': 0}, {'门店确认数量': 7},
            {'商品编码': 'second', '商品名称': '原名称'}, {'商品编码': 'second', '商品名称': '别名'}])
        conflicts = duplicates.find(document['scan']['rows'])
        self.assertEqual(len(conflicts), 2)
        with self.assertRaises(ValueError) as caught:
            batch.prepare(self.f.path, [document])
        message = str(caught.exception)
        for text in ('实际出错来源：', document['scan']['files'][0]['name'], self.f.h['number'],
                     '共2款', '0001', 'second', '业务明细', '第2行', '第3行', '数量=0', '数量=7', '商品名称=别名'):
            self.assertIn(text, message)
        self.assertEqual(service.history(self.f.path, self.f.h, 'return'), [])

    def test_explicit_zero_and_positive_keep_the_selected_whole_row_without_adding(self):
        for index, value in ((0, '0'), (1, '7')):
            with self.subTest(value=value):
                document = self.document(); original = copy.deepcopy(document)
                resolved = duplicates.resolve(document, self.choices(document, index))
                plan = batch.prepare(self.f.path, [resolved])['plans'][0]
                row = plan['payload']['raw_rows'][0]
                self.assertEqual(row['quantity'], value)
                self.assertEqual(row['base_quantity'], value)
                self.assertEqual(row['sources'], [document['scan']['rows'][index]['source']])
                audit = row['duplicate_resolution']
                self.assertEqual(len(audit['candidates']), 2)
                self.assertEqual(audit['selected_source'], row['sources'][0])
                self.assertEqual(document, original)

    def test_payment_quantity_and_amount_follow_the_same_selected_row(self):
        document = self.document('payment', [{'付款单商品数量': 2, '实际金额': '4.10'},
                                              {'付款单商品数量': 3, '实际金额': '6.20'}])
        resolved = duplicates.resolve(document, self.choices(document, 1))
        row = batch.prepare(self.f.path, [resolved])['plans'][0]['payload']['raw_rows'][0]
        self.assertEqual((row['quantity'], row['amount'], row['currency']), ('3', '6.2', 'CNY'))

    def test_blank_quantity_is_not_treated_as_an_explicit_zero(self):
        document = self.document(items=[{'门店确认数量': None}, {'门店确认数量': 0}])
        self.assertEqual(len(duplicates.find(document['scan']['rows'])), 1)
        resolved = duplicates.resolve(document, self.choices(document, 0))
        payload = batch.prepare(self.f.path, [resolved])['plans'][0]['payload']
        self.assertIsNone(payload['raw_rows'][0]['quantity'])
        self.assertEqual(payload['rows'][0]['quantity_origin'], 'blank')
        self.assertIsNone(payload['raw_rows'][0]['duplicate_resolution']['candidates'][0]['facts']['quantity'])

    def test_all_choices_required_and_stale_candidates_or_sources_rejected(self):
        document = self.document(); choices = self.choices(document, 1)
        for changed in ({}, {next(iter(choices)): 'unrecognized'}):
            with self.assertRaisesRegex(ValueError, '重新|全部'):
                duplicates.resolve(document, changed)
        for field in ('quantity', 'name'):
            changed = copy.deepcopy(document); changed['scan']['rows'][0][field] = 'changed'
            with self.assertRaisesRegex(ValueError, '变化'):
                duplicates.resolve(changed, choices)
        changed = copy.deepcopy(document); changed['scan']['rows'][0]['source']['sha'] = 'different'
        with self.assertRaisesRegex(ValueError, '变化'):
            duplicates.resolve(changed, choices)

    def test_partial_resolution_cannot_silently_drop_a_second_conflict(self):
        document = self.document(items=[{}, {'门店确认数量': 7}, {'商品编码': 'other'},
                                         {'商品编码': 'other', '门店确认数量': 8}])
        choices = self.choices(document, 1); choices.pop(next(iter(choices)))
        with self.assertRaisesRegex(ValueError, '全部'):
            duplicates.resolve(document, choices)

    def test_same_choice_is_idempotent_and_original_file_and_signed_facts_are_unchanged(self):
        document = self.document(); path = Path(document['scan']['files'][0]['path'])
        original_hash = hashlib.sha256(path.read_bytes()).hexdigest(); signed = self.f.old_tables()
        choices = self.choices(document, 1)
        first = batch.prepare(self.f.path, [duplicates.resolve(document, choices)])
        service.commit(self.f.path, first)
        again = batch.prepare(self.f.path, [duplicates.resolve(document, choices)])
        self.assertEqual(again['plans'][0]['digest'], first['plans'][0]['digest'])
        self.assertFalse(again['plans'][0]['changed'])
        self.assertFalse(service.commit(self.f.path, again)[0]['changed'])
        self.assertEqual(len(service.history(self.f.path, self.f.h, 'return')), 1)
        self.assertEqual(self.f.old_tables(), signed)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), original_hash)

    def test_nonconflicting_duplicates_and_first_seen_order_keep_existing_behavior(self):
        document = self.document(items=[{'商品编码': 'before'}, {}, {'商品编码': 'after'},
                                         {'门店确认数量': 8}, {'商品编码': 'before'}])
        resolved = duplicates.resolve(document, self.choices(document, 1))
        plan = batch.prepare(self.f.path, [resolved])['plans'][0]
        self.assertEqual([r['sku'] for r in plan['payload']['raw_rows']], ['before', '0001', 'after'])
        self.assertEqual(plan['duplicates'], 1)
        self.assertEqual(len(plan['payload']['raw_rows'][0]['sources']), 2)

    def test_other_warehouse_and_document_number_are_not_merged(self):
        document = self.document(); rows = document['scan']['rows']
        for key in ('signed_number', 'store', 'warehouse', 'document_number', 'document_date', 'revision'):
            changed = copy.deepcopy(rows); changed[1][key] = 'different'
            with self.subTest(key=key):
                self.assertEqual(duplicates.find(changed), [])
        document['scan']['rows'][1]['document_number'] = 'D002'
        with self.assertRaisesRegex(ValueError, '不同单号'):
            batch.prepare(self.f.path, [document])

    def test_conflicting_second_document_names_itself_and_does_not_write_first(self):
        first = self.document(items=[{}]); second = self.document()
        with self.assertRaises(ValueError) as caught:
            # Another warehouse is a different target/stage and reaches the
            # conflict checker after the valid first document was previewed.
            other = self.f.heads[1]
            for row in second['scan']['rows']:
                row.update(signed_number=other['number'], warehouse=other['warehouse'], sku='0002')
            second['target'] = other
            batch.prepare(self.f.path, [first, second])
        self.assertIn(second['scan']['files'][0]['name'], str(caught.exception))
        self.assertNotIn(first['scan']['files'][0]['name'], str(caught.exception))
        for head in self.f.heads:
            self.assertEqual(service.history(self.f.path, head, 'return'), [])

    def test_file_changes_after_resolution_still_fail_commit(self):
        document = self.document(); resolved = duplicates.resolve(document, self.choices(document, 1))
        plan = batch.prepare(self.f.path, [resolved]); path = Path(plan['files'][0]['path'])
        path.write_bytes(path.read_bytes() + b'changed-after-review')
        with self.assertRaisesRegex(ValueError, '文件在预览后变化'):
            service.commit(self.f.path, plan)
        self.assertEqual(service.history(self.f.path, self.f.h, 'return'), [])


if __name__ == '__main__':
    unittest.main()
