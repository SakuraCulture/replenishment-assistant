import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from aoxiang_acquisition import prepare, manifest, freeze_manifest, verify_loaded
from aoxiang_jobs import Jobs
from aoxiang_directory import file_hash
from aoxiang_client import ComponentError
from unittest.mock import Mock


def configuration():
    return {'configured': True, 'enabled': True, 'display_name': '测试商家',
            'account_binding': {'account_id': 'A', 'merchant_code': 'M'}, 'store_mapping_version': 'mapping-1',
            'targets': [{'buyerWarehouseCode': code, 'displayName': name} for code, name in [('W2', '门店二'), ('W1', '门店一')]]}


def complete(jobs, root, job, states=('SUCCEEDED', 'SUCCEEDED')):
    record = {'id': job['id'], 'fingerprint': job['fingerprint'], 'request': job['command'],
              'state': 'SUCCEEDED' if all(s == 'SUCCEEDED' for s in states) else 'COMPLETED_WITH_ERRORS',
              'sequence': 2, 'items': []}
    for index, (code, state) in enumerate(zip(job['command']['scope']['warehouse_codes'], states)):
        file = root / 'command-files' / job['id'] / f'{index}-temp.xlsx'
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(('test payload ' + code).encode())
        digest = file_hash(file)
        target = file.with_name(f'{index}-{digest}.xlsx')
        file.rename(target)
        result = {'warehouse_code': code, 'run_id': f'run-{index}', 'file': str(target.resolve()),
                  'sha256': digest, 'bytes': target.stat().st_size, 'rows': index + 1,
                  'account_binding': job['command']['account_binding'], 'reused': False}
        record['items'].append({'index': index, 'warehouse_code': code, 'run_id': f'run-{index}',
                                'state': state, 'result': result if state == 'SUCCEEDED' else None, 'error': None})
    jobs.observe(job['id'], record)
    return jobs.get(job['id'])


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.jobs = Jobs(self.root / 'jobs.sqlite3')

    def tearDown(self):
        self.temp.cleanup()

    def test_conflict_carries_exact_original_day_and_overlapping_stores_without_creating_a_job(self):
        from aoxiang_acquisition import PendingAcquisition
        old=prepare(self.jobs,configuration(),['W2'],'refresh',day='2026-11-01')
        with self.assertRaises(PendingAcquisition) as caught:prepare(self.jobs,configuration(),['W2','W1'],'refresh',day='2026-11-11')
        error=caught.exception
        self.assertEqual(error.blockers[0]['id'],old['id']);self.assertEqual(error.blockers[0]['day'],'2026-11-01')
        self.assertEqual(error.blockers[0]['overlap'],['W2']);self.assertIn('门店二（W2）',str(error))
        self.assertIn('重新导出所选门店',str(error));self.assertEqual(len(self.jobs.recent()),1)
        other=prepare(self.jobs,configuration(),['W1'],'refresh',day='2026-11-11')
        self.assertNotEqual(old['id'],other['id'])

    def make(self, codes=('W2', 'W1'), states=('SUCCEEDED', 'SUCCEEDED')):
        job = prepare(self.jobs, configuration(), codes, 'reuse', day='2026-11-02')
        return complete(self.jobs, self.root, job, states)

    def test_requested_order_manifest_and_exact_snapshot_reuse(self):
        job = self.make(('W1', 'W2'))
        value = manifest(job, self.root)
        self.assertEqual([v['warehouse_code'] for v in value['files']], ['W1', 'W2'])
        first = freeze_manifest(value, self.root)
        self.assertEqual(freeze_manifest(value, self.root), first)
        self.assertEqual(self.jobs.recent()[0]['id'], job['id'])
        self.assertEqual(Jobs(self.jobs.path).get(job['id']), job)

    def test_out_of_order_or_missing_item_never_sorted_to_hide_difference(self):
        job = self.make()
        job['record']['items'].reverse()
        with self.assertRaisesRegex(ValueError, '顺序'):
            manifest(job, self.root)
        job['record']['items'].pop()
        with self.assertRaisesRegex(ValueError, '不完整'):
            manifest(job, self.root)

    def test_partial_failure_preserved_not_adopted(self):
        value = manifest(self.make(states=('FAILED', 'SUCCEEDED')), self.root)
        self.assertEqual([i['warehouse_code'] for i in value['files']], ['W1'])
        self.assertEqual(value['omitted'], [{'index': 0, 'warehouse_code': 'W2', 'state': 'FAILED', 'error': None}])
        with self.assertRaisesRegex(ValueError, '没有可采用'):
            manifest(self.make(states=('FAILED', 'FAILED')), self.root)

    def test_changed_bytes_same_size_and_wrong_scope_blocked(self):
        job = self.make()
        item = job['record']['items'][0]['result']
        path = Path(item['file'])
        path.write_bytes(b'x' * path.stat().st_size)
        with self.assertRaisesRegex(ValueError, '文件变化'):
            manifest(job, self.root)
        job = self.make()
        job['record']['items'][0]['result']['warehouse_code'] = 'W9'
        with self.assertRaisesRegex(ValueError, '关联不符'):
            manifest(job, self.root)

    def test_other_command_path_cannot_impersonate_valid_hash(self):
        job = self.make()
        item = job['record']['items'][0]['result']
        copy_path = self.root / 'latest.xlsx'
        copy_path.write_bytes(Path(item['file']).read_bytes())
        item['file'] = str(copy_path)
        with self.assertRaisesRegex(ValueError, '原件位置'):
            manifest(job, self.root)

    def test_loaded_hash_order_and_errors_checked_without_business_mutation(self):
        value = manifest(self.make(), self.root)
        loaded = {'intake': {'files': [{'path': i['file'], 'error': None} for i in value['files']],
                             'hashes': {i['file']: i['sha256'] for i in value['files']}}, 'normalized': [('sentinel', {'qty': 24})]}
        before = copy.deepcopy(loaded)
        self.assertIs(verify_loaded(value, loaded), loaded)
        self.assertEqual(loaded, before)
        loaded['intake']['files'].reverse()
        with self.assertRaisesRegex(ValueError, '顺序'):
            verify_loaded(value, loaded)
        loaded = copy.deepcopy(before)
        loaded['intake']['files'][0]['error'] = 'test invalid header'
        with self.assertRaisesRegex(ValueError, 'test invalid header'):
            verify_loaded(value, loaded)

    def test_active_receipts_or_invalid_selection_cannot_be_adopted(self):
        for codes in [[], ['W9'], ['W2', 'W2']]:
            with self.assertRaises(ValueError):
                prepare(self.jobs, configuration(), codes, 'reuse', day='2026-11-02')
        job = self.make()
        job['record']['state'] = 'PAUSE_REQUESTED'
        with self.assertRaisesRegex(ValueError, '安全暂停'):
            manifest(job, self.root)

    def test_unknown_dispatch_cannot_be_replaced_by_new_id(self):
        first = prepare(self.jobs, configuration(), ['W2'], 'reuse', day='2026-11-02')
        with self.assertRaisesRegex(ValueError, '不能另建任务'):
            prepare(self.jobs, configuration(), ['W2', 'W1'], 'refresh', day='2026-11-02')
        other = prepare(self.jobs, configuration(), ['W1'], 'reuse', day='2026-11-02')
        self.assertNotEqual(first['id'], other['id'])

    def test_unaccepted_cancel_requires_definitive_missing_not_timeout(self):
        first = prepare(self.jobs, configuration(), ['W2'], 'reuse', day='2026-11-02')
        client = Mock()
        client.request.side_effect = ComponentError('RESPONSE_UNKNOWN', 'timeout')
        with self.assertRaises(ComponentError):
            self.jobs.cancel_unaccepted(client, first['id'])
        self.assertEqual(self.jobs.get(first['id'])['state'], 'PREPARED')
        client.request.side_effect = ComponentError('COMMAND_NOT_FOUND', 'missing')
        self.assertEqual(self.jobs.cancel_unaccepted(client, first['id'])['state'], 'CANCELLED')
        self.assertEqual(self.jobs.unfinished(), [])
        with self.assertRaisesRegex(ValueError, '不能再次派发'):
            self.jobs.dispatch(client, first['id'])
        prepare(self.jobs, configuration(), ['W2'], 'reuse', day='2026-11-02')


if __name__ == '__main__':
    unittest.main()
