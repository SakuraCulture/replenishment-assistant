import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import automation_protocol as protocol
from automation_bridge import response
from automation_tasks import Journal
from workbench_actions import budget_value, record_settings, recalculation_impact, plan_record
import parameter_schema as schema
import store_parameter_import as spi
import work_repository as wr


class AutomationContractTests(unittest.TestCase):
    def test_public_surface_excludes_persistence_and_arbitrary_execution(self):
        names = {x['name'] for x in protocol.public_tools()}
        self.assertEqual(len(names), 22)
        self.assertTrue({'query_order_funnel','order_funnel_results','export_order_funnel','store_history_review',
                         'start_stock_diagnosis','stock_diagnosis_results','adopt_stock_diagnosis','undo_stock_diagnosis'}<=names)
        for name in ['execute_python', 'sql', 'confirm_normal', 'delete_orders', 'save_stores']:
            with self.assertRaises(protocol.CommandError): protocol.validate_call(name, {})

    def test_types_missing_unknown_nan_and_duplicate_fields_rejected(self):
        for args in [{'kind': 'tasks', 'limit': True}, {'kind': 'tasks', 'limit': 101}, {'kind': 'tasks', 'limit': 1.2}, {'kind': 'tasks', 'sql': 'x'}, {}]:
            with self.subTest(args=args), self.assertRaises(protocol.CommandError): protocol.validate_call('find_resources', args)
        for raw in [b'{"x":1,"x":2}', b'{"x":NaN}', b'not-json', b'x' * (protocol.MAX_FRAME + 1)]:
            with self.assertRaises(protocol.CommandError): protocol.decode(raw)

    def test_store_order_never_sorted_and_duplicates_rejected(self):
        args = {'context_token': 'x', 'operation_key': 'y', 'mode': 'trial', 'stores': ['B', 'A']}
        self.assertEqual(protocol.validate_call('prepare_calculation', args)['stores'], ['B', 'A'])
        args['stores'] = ['A', 'A']
        with self.assertRaises(protocol.CommandError): protocol.validate_call('prepare_calculation', args)

    def test_mcp_initialize_discover_call_and_error(self):
        class Client:
            def call(self, name, args):
                protocol.validate_call(name, args)
                return {'message': '未签发', 'amount': 0}
        ready = [False]; c = Client()
        def call(method, **params): return response({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}, c, ready)
        self.assertIn('error', call('tools/list'))
        self.assertEqual(call('initialize', protocolVersion='2025-11-25')['result']['protocolVersion'], '2025-11-25')
        self.assertEqual(len(call('tools/list')['result']['tools']), 22)
        self.assertEqual(call('tools/call', name='workbench_context')['result']['structuredContent']['amount'], 0)
        self.assertTrue(call('tools/call', name='save')['result']['isError'])
        self.assertIsNone(response({'jsonrpc': '2.0', 'method': 'tools/call', 'params': {'name': 'workbench_context'}}, c, ready))

    def test_budget_original_rules(self):
        self.assertIsNone(budget_value(1, '', None))
        self.assertEqual(budget_value(2, '0', {}), 0)
        self.assertEqual(budget_value(2, '3000.00', {}), 3000)
        for mode, raw in [(0, ''), (2, ''), (2, 'NaN'), (2, '-1'), (2, '0.001'), (2, '1000000001')]:
            with self.subTest(raw=raw), self.assertRaises(ValueError): budget_value(mode, raw, {})

    def test_parameter_scope_budget_route_and_priority_are_distinct(self):
        record = spi.base(); record['ui'].update(mode=2, amount='10')
        with self.assertRaises(ValueError): record_settings(record, None, None, {})
        settings = record_settings(record, {'revision': 'B'}, None, {'state': 'unknown'})
        self.assertEqual(settings['budget'], 10)
        self.assertEqual(settings['parameter_config'], schema.standard())
        self.assertEqual(settings['admission'], {'state': 'unknown'})

    def test_recalculation_impact_exact_original_order(self):
        rows = {'A': {'new_products': {'items': [1]}}, 'B': {'preissue': {'edits': [1]}}}
        before = copy.deepcopy(rows); impact = recalculation_impact(rows, ['B', 'A'])
        self.assertEqual(impact['new_products'], ['A']); self.assertEqual(list(impact['manual']), ['B'])
        self.assertEqual(rows, before)


class AutomationJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.repo = wr.Repository(self.temp.name)
        self.j = Journal(self.repo.path, 'first')
    def tearDown(self): self.temp.cleanup()

    def test_operation_key_replay_is_same_task_and_changed_args_rejected(self):
        a, fresh = self.j.create('one', {'stores': ['B', 'A']}, 'prepare')
        b, fresh2 = self.j.create('one', {'stores': ['B', 'A']}, 'prepare')
        self.assertTrue(fresh); self.assertFalse(fresh2); self.assertEqual(a, b)
        with self.assertRaises(protocol.CommandError): self.j.create('one', {'stores': ['A', 'B']}, 'prepare')

    def test_interrupted_session_not_reported_as_completed(self):
        a, _ = self.j.create('one', {}, 'prepare')
        j2 = Journal(self.repo.path, 'second')
        self.assertEqual(j2.get(a['task_id'])['state'], 'interrupted')
        self.assertEqual(self.repo.list_jobs(), [])

    def test_events_are_ordered_incremental_and_metadata_only(self):
        a, _ = self.j.create('one', {}, 'calculate')
        for state in ('running', 'stopping', 'partial'): self.j.update(a['task_id'], state=state)
        events = self.j.events(a['task_id']); self.assertEqual([r['state'] for r in events], ['running', 'stopping', 'partial'])
        self.assertEqual([r['state'] for r in self.j.events(a['task_id'], events[0]['cursor'])], ['stopping', 'partial'])
        self.assertEqual(self.repo.list_jobs(), [])


if __name__ == '__main__': unittest.main()
