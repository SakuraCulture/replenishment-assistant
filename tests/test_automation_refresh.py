import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import automation_queries as queries
import automation_protocol as protocol
import work_repository as wr
import store_parameter_import as spi
from automation_bridge import response
from scheduled_replenishment import Repository
from version_info import VERSION


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.work = wr.Repository(self.root); self.repo = Repository(self.work.path)
    def tearDown(self): self.tmp.cleanup()

    def test_schedule_query_absent_does_not_create_schema(self):
        before = self.work.path.read_bytes()
        for kind in ('schedules', 'scheduled_runs'):
            self.assertEqual(queries.scheduled_resources(self.work.path, {'kind': kind})['items'], [])
        with self.assertRaises(protocol.CommandError): queries.scheduled_status(self.work.path, {'run_id': 'missing'})
        self.assertEqual(before, self.work.path.read_bytes())

    def test_schedule_pages_events_order_and_readonly(self):
        s = self.repo.save_schedule(dict(name='合成安排', plan_id='p', kind='once', date='2099-01-01', time='06:00', enabled=False))
        run = self.repo.manual(s)
        for n in range(4):
            self.repo.change(run['id'], message='事件' + str(n), state='REVIEW',
                stores=[dict(code='B', name='乙', state='READY', message='完成'), dict(code='A', name='甲', state='FAILED', message='合成失败')],
                external={'do_not_expose': 'secret'})
        before = self.work.path.read_bytes()
        with patch.object(Repository, 'ensure', side_effect=AssertionError('write')), patch.object(Repository, 'value', side_effect=AssertionError('heavy blob')):
            runs = queries.scheduled_resources(self.work.path, {'kind': 'scheduled_runs', 'limit': 1})
            first = queries.scheduled_status(self.work.path, {'run_id': run['id'], 'limit': 1})
            second = queries.scheduled_status(self.work.path, {'run_id': run['id'], 'limit': 1, 'offset': first['stores']['next_offset'], 'after': first['next_after']})
        self.assertEqual(runs['items'][0]['ready_stores'], 1)
        self.assertEqual(first['stores']['items'][0]['code'], 'B')
        self.assertEqual(second['stores']['items'][0]['code'], 'A')
        self.assertIsNone(second['stores']['next_offset'])
        self.assertGreater(second['next_after'], first['next_after'])
        self.assertTrue(first['has_more_events'])
        self.assertNotIn('do_not_expose', json.dumps([runs, first, second]))
        self.assertEqual(before, self.work.path.read_bytes())

    def test_current_and_saved_parameters_have_distinct_values_and_scope(self):
        saved = spi.base(); saved['ui'].update(mode=1, primary='山东仓', secondary=None)
        self.work.save_stores({'001': saved})
        current = copy.deepcopy(saved); current['preset']['config']['warehouses']['山东仓'] = [11, 2]
        current['ui'].update(mode=2, amount='10')
        host = SimpleNamespace(work_repository=self.work, store_widgets={'001': {}}, states={'001': {'name': '合成门店'}}, current_parameter_record=lambda _: current)
        before = self.work.path.read_bytes()
        a = queries.parameter_result(host, {'stores': ['001']})
        b = queries.parameter_result(host, {'stores': ['001'], 'scope': 'defaults'})
        get = lambda r: next(f['value'] for f in r['stores'][0]['parameters'] if f['path'] == ['warehouses', '山东仓', 0])
        self.assertEqual(get(a), 11); self.assertNotEqual(get(a), get(b))
        self.assertNotEqual(a['stores'][0]['parameter_signature'], b['stores'][0]['parameter_signature'])
        self.assertEqual(a['stores'][0]['budget']['mode'], '限额补货')
        self.assertEqual(b['stores'][0]['budget']['mode'], '不限预算')
        self.assertIn('新品铺设', a['stores'][0]['policies'])
        self.assertEqual(self.work.path.read_bytes(), before)
        with self.assertRaises(protocol.CommandError): queries.parameter_result(host, {'stores': ['not-code'], 'scope': 'defaults'})

    def test_file_registration_is_not_validation(self):
        host = SimpleNamespace(sales=SimpleNamespace(paths=['a.xlsx']), loaded=None, input_dirty=False)
        self.assertIsNone(queries.file_resources(host)[0]['check_report'])
        host.loaded = {'intake': {'files': [{'path': 'a.xlsx', 'error': 'bad'}]}}
        self.assertEqual(queries.file_resources(host)[0]['check_report']['error'], 'bad')
        host.input_dirty = True
        self.assertIsNone(queries.file_resources(host)[0]['check_report'])

    def test_no_new_scheduler_write_contract(self):
        for name in ('start_schedule', 'resume_schedule', 'delete_schedule', 'refresh_catalogs'):
            with self.assertRaises(protocol.CommandError): protocol.validate_call(name, {})
        for args in ({'run_id':'r','limit':101}, {'run_id':'r','after':-1}, {'run_id':'r','start':True}):
            with self.assertRaises(protocol.CommandError): protocol.validate_call('scheduled_run_status', args)
        tool = next(t for t in protocol.public_tools() if t['name'] == 'scheduled_run_status')
        self.assertTrue(tool['annotations']['readOnlyHint'])

    def test_cli_and_legacy_mcp_offline(self):
        bridge = str(Path(__file__).resolve().parents[1] / 'src/automation_bridge.py')
        def run(*args, text=None):
            r = subprocess.run([sys.executable, '-X', 'utf8', bridge, *args], input=text, capture_output=True, encoding='utf-8', timeout=15)
            return r, json.loads(r.stdout)
        r, data = run('version'); self.assertEqual(r.returncode, 0); self.assertEqual(data['result']['version'], VERSION)
        r, data = run('--connection', str(self.root/'missing'), 'doctor')
        self.assertEqual(r.returncode, 2); self.assertEqual(data['error']['code'], 'NOT_CONNECTED')
        invalid = self.root/'invalid.json'; invalid.write_text('{"token":"never-print"}', 'utf-8')
        r, data = run('--connection', str(invalid), 'doctor')
        self.assertEqual(data['error']['code'], 'INVALID_CONNECTION'); self.assertNotIn('never-print', r.stdout + r.stderr)
        r, data = run('--connection', str(invalid), 'call', 'effective_parameters', '--arguments', '{"stores":[],"stores":[]}')
        self.assertEqual(data['error']['code'], 'INVALID_JSON')
        request = json.dumps(dict(jsonrpc='2.0', id=1, method='initialize', params={})) + '\n'
        r, data = run('--connection', str(invalid), text=request)
        self.assertEqual(r.returncode, 0); self.assertEqual(data['result']['serverInfo']['version'], VERSION)


if __name__ == '__main__': unittest.main()
