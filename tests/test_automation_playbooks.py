import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from automation_playbooks import PLAYBOOKS, render, store_codes


class RecipeContract(unittest.TestCase):
    def setUp(self):
        self.context = dict(instance_id='isolated-window', business_date='2026-11-04', context_token='exact-state')
        self.values = dict(stores=['B'], task_id='exact-task', warehouse='山东仓', sku='001', order_id='exact-order-SD', days_a=5, days_b=7, reviewed=True)

    def test_every_recipe_is_bound_and_does_not_authorize_formal_writes(self):
        for case, *_ in PLAYBOOKS:
            with self.subTest(case=case):
                text = render(case, self.context, self.values)
                for boundary in ('exact-state', 'isolated-window', '不保存草稿', '不签发', '不采购', '不删除', 'next_offset', 'mode') if case == 'calculate' else ('exact-state', 'isolated-window', '不保存草稿', '不签发', '不采购', '不删除', 'next_offset'):
                    self.assertIn(boundary, text)

    def test_codes_keep_order_and_exact_identity(self):
        self.assertEqual(store_codes('B，001;A', {'A': 1, 'B': 2, '001': 3}), ['B', '001', 'A'])
        for text in ('', 'A,A', '门店A', '01'):
            with self.subTest(text=text), self.assertRaises(ValueError): store_codes(text, {'A': 1, '001': 2})

    def test_scope_cannot_silently_expand(self):
        for case in ('compare', 'explain'):
            with self.assertRaises(ValueError): render(case, self.context, {**self.values, 'stores': ['A', 'B']})
        with self.assertRaises(ValueError): render('calculate', self.context, {**self.values, 'stores': []})
        with self.assertRaises(ValueError): render('review', self.context, {**self.values, 'task_id': ''})

    def test_compare_and_adopt_are_distinct_authorizations(self):
        self.assertIn('不要采用任何一次结果', render('compare', self.context, self.values))
        with self.assertRaises(ValueError): render('adopt', self.context, {**self.values, 'reviewed': False})
        for value in (0, -1, float('nan'), True, 366, 7):
            with self.subTest(value=value), self.assertRaises(ValueError): render('compare', self.context, {**self.values, 'days_a': value})

    def test_history_never_falls_back_to_current_parameters(self):
        text = render('history', self.context, self.values)
        self.assertIn('不使用 effective_parameters', text)
        self.assertIn('exact-order-SD', text)
        self.assertIn('"sku": "001"', text)
        self.assertNotIn('"task_id"', text)


if __name__ == '__main__': unittest.main()
