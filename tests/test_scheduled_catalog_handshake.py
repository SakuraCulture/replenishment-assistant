"""Scheduled acquisition must answer the real catalog protocol's WAIT_HOST step."""
import copy
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from scheduled_platforms import Platforms
from runtime_support import Cancelled


class Client:
    def __init__(self, warehouses, *, reused=False, cancel=None):
        self.calls = []
        self.reused, self.cancel = reused, cancel
        self.warehouses = warehouses
        self.poll = 0
        self.answers = {}

    def request(self, action, **kw):
        self.calls.append((action, copy.deepcopy(kw)))
        if action == 'sync.start':
            return {'batch_id': 'original-batch', 'reused': self.reused}
        if action in ('sync.resume', 'sync.stop', 'sync.acknowledge_adoption'):
            return {}
        if action == 'sync.provide_observation':
            if kw['token'] in self.answers:
                raise AssertionError('Same observation request answered twice')
            self.answers[kw['token']] = kw['observation']
            return {'accepted': True}
        if action != 'sync.status':
            raise AssertionError(action)
        self.poll += 1
        waiting = 2 <= self.poll <= 3
        complete = self.poll >= 4
        if complete and self.cancel is None:
            if set(self.answers) != {'token-' + w for w in self.warehouses}:
                raise AssertionError('HOST_TIMEOUT: observation was never supplied')
        if waiting and self.cancel:
            self.cancel.set()
        items = [{'id': 'item-' + w, 'account': {'warehouse': w},
                  'stage': 'WAIT_HOST' if waiting else 'QUEUED',
                  'observationRequest': 'token-' + w, 'observation': None,
                  'error': None, 'receipt': None, 'adoption': None}
                 for w in self.warehouses]
        return {'running': not complete and not (self.reused and self.poll == 1),
                'batches': [{'items': items}],
                'active': None if complete else {'phase': {'message': '连接浏览器'}}}


class CatalogHandshakeTests(unittest.TestCase):
    def run_case(self, warehouses=('山东仓',), *, reused=False, cancel=None, observe=None):
        stopped = cancel or threading.Event()
        client = Client(warehouses, reused=reused, cancel=cancel)
        external = {'catalog_operation': 'original-operation',
                    'catalog_items': [{'warehouse': w, 'expected_catalog_digest': w + '-digest'}
                                      for w in warehouses]}
        repo = SimpleNamespace(get=lambda _: {'external': copy.deepcopy(external)},
                               value=lambda *_: None, change=Mock())
        runner = SimpleNamespace(repo=repo, rid='run', epoch=1, update=Mock())
        platform = Platforms(Path('.'), Path('isolated.sqlite3'), None, None, stopped)
        platform.jst_client = lambda: client
        observation = observe or (lambda path, w: {'warehouse': w, 'state': [w, 1],
                                                   'included': [w + '-locked'], 'observed_at': 'fixed'})
        with patch('scheduled_platforms.stock_observation', side_effect=observation) as reader, \
             patch('scheduled_platforms.time.sleep'):
            try:
                result = platform.catalogs(runner, {'warehouses': list(warehouses)})
            except Cancelled:
                result = 'cancelled'
        return client, runner, reader, result

    def test_wait_host_answer_once_and_clear_progress(self):
        client, runner, reader, result = self.run_case()
        self.assertEqual(result, {})  # This protocol fixture intentionally has no downloaded file.
        self.assertEqual(reader.call_count, 1)
        answer = next(kw for action, kw in client.calls if action == 'sync.provide_observation')
        self.assertEqual(answer, {'batch_id': 'original-batch', 'item_id': 'item-山东仓',
                                 'token': 'token-山东仓', 'observation': {
                                     'warehouse': '山东仓', 'state': ['山东仓', 1],
                                     'included': ['山东仓-locked'], 'observed_at': 'fixed'}})
        messages = [c.args[1] for c in runner.update.call_args_list]
        self.assertTrue(any('查询依据' in m and '尚未提交' in m for m in messages))

    def test_two_warehouses_keep_order_and_own_observation(self):
        client, _, reader, _ = self.run_case(('河北仓', '山东仓'))
        self.assertEqual([c.args[1] for c in reader.call_args_list], ['河北仓', '山东仓'])
        answers = [kw for action, kw in client.calls if action == 'sync.provide_observation']
        self.assertEqual([x['observation']['warehouse'] for x in answers], ['河北仓', '山东仓'])

    def test_resume_uses_original_batch_and_operation(self):
        client, _, _, _ = self.run_case(reused=True)
        start = next(kw for action, kw in client.calls if action == 'sync.start')
        resume = next(kw for action, kw in client.calls if action == 'sync.resume')
        self.assertEqual(start['operation_id'], 'original-operation')
        self.assertEqual(resume, {'batch_id': 'original-batch', 'item_ids': ['item-山东仓']})

    def test_stop_does_not_supply_observation_or_resume_query(self):
        client, _, reader, result = self.run_case(cancel=threading.Event())
        self.assertEqual(result, 'cancelled')
        self.assertEqual(reader.call_count, 0)
        self.assertEqual([a for a, _ in client.calls].count('sync.stop'), 1)
        self.assertNotIn('sync.provide_observation', [a for a, _ in client.calls])

    def test_cancel_during_observation_does_not_acknowledge(self):
        stop = threading.Event()
        def observe(path, wh):
            stop.set()
            return {'warehouse': wh}
        # Keep client polls uncancelled, but cancel in the read checkpoint.
        with patch('scheduled_platforms.checkpoint', wraps=__import__('runtime_support').checkpoint):
            client = Client(('山东仓',))
            repo = SimpleNamespace(get=lambda _: {'external': {'catalog_operation': 'op', 'catalog_items': [{'warehouse': '山东仓', 'expected_catalog_digest': None}]}},
                                   value=lambda *_: None, change=Mock())
            runner = SimpleNamespace(repo=repo, rid='run', epoch=1, update=Mock())
            platform = Platforms(Path('.'), Path('isolated.sqlite3'), None, None, stop)
            platform.jst_client = lambda: client
            with patch('scheduled_platforms.stock_observation', side_effect=observe), \
                 patch('scheduled_platforms.time.sleep'):
                with self.assertRaises(Cancelled):
                    platform.catalogs(runner, {})
        self.assertNotIn('sync.provide_observation', [a for a, _ in client.calls])


if __name__ == '__main__':
    unittest.main()
