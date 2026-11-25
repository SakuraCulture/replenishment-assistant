"""Durability and publication checks for the timed report workbench."""
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from business_daily import PendingInputs
from timed_exports import DATABASE, Repository, Runner, TZ, preview, sha256


class Backend:
    def __init__(self):
        self.missing = False
        self.next_version = 'source-1'
        self.renders = []

    def lock(self, run):
        return nullcontext()

    def prepare(self, run, progress, cancel):
        if self.missing:
            raise PendingInputs(['缺河北仓快照'])
        basis = run['frozen'] or {'version': self.next_version, 'day': run['dates'][1]}
        return basis, basis['version']

    def render(self, run, prepared, target, progress, cancel):
        self.renders.append(prepared)
        target.write_text(prepared, encoding='utf-8')
        return {'prepared': prepared}


class TimedExportsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.clock = datetime(2026, 11, 25, 23, 58, tzinfo=TZ)
        self.repo = Repository(self.root / DATABASE, clock=lambda: self.clock)
        self.output = self.root / 'export'
        self.plan = {'name': '两仓日报', 'type': 'business_daily', 'date_rule': 'yesterday', 'time': '23:59',
                     'kind': 'weekly', 'weekdays': list(range(7)), 'date': '2026-11-25',
                     'wait_minutes': 30, 'directory': str(self.output), 'enabled': True}
        self.backend = Backend()
        self.runner = Runner(self.repo, self.backend, threading.Event())

    def tearDown(self):
        self.tmp.cleanup()

    def test_repeated_wakeup_and_resume_after_midnight_pin_the_original_day(self):
        self.assertEqual([r['dates'][0] for r in preview(self.plan, self.clock)],
                         ['2026-11-24', '2026-11-25', '2026-11-26'])
        plan = self.repo.save_plan(self.plan)
        self.clock += timedelta(minutes=2)
        run, = self.repo.due()
        self.assertEqual(run['dates'], ['2026-11-24', '2026-11-24'])
        self.assertFalse(self.repo.due())
        self.backend.missing = True
        self.runner.execute(run['id'])
        self.assertEqual(self.repo.inspect(run['id'])[0]['state'], 'WAITING')
        self.assertIsNone(self.repo.next())
        self.assertEqual(self.repo.inspect(run['id'])[0]['message'], '缺河北仓快照')
        current, = self.repo.specs()
        self.repo.save_plan({**current, 'enabled': False, 'date_rule': 'today'}, current)
        self.assertEqual(self.repo.inspect(run['id'])[0]['plan']['revision'], plan['revision'])
        self.clock += timedelta(seconds=16)
        self.backend.missing = False
        reopened = Repository(self.repo.path, clock=lambda: self.clock)
        self.assertEqual(reopened.next(), run['id'])
        Runner(reopened, self.backend, threading.Event()).execute(run['id'])
        final, file = self.repo.inspect(run['id'])
        self.assertEqual((final['state'], final['frozen']['day']), ('COMPLETE', '2026-11-24'))
        self.assertEqual(len(list(self.output.glob('*.xlsx'))), 1)
        self.assertEqual(sha256(file['path']), file['sha256'])

    def test_published_file_without_receipt_only_finishes_original_record(self):
        run = self.repo.manual(self.plan, 'request-A')
        self.assertEqual(self.repo.manual(self.plan, 'request-A')['id'], run['id'])
        with patch.object(self.repo, 'confirmed', side_effect=OSError('模拟发布后应答中断')):
            self.runner.execute(run['id'])
        original, artifact = self.repo.inspect(run['id'])
        self.assertEqual(original['state'], 'REVIEW')
        self.assertNotIn('published_at', artifact)
        self.assertEqual(sha256(artifact['path']), artifact['sha256'])
        self.repo.request(run['id'])
        self.backend.next_version = 'source-2'
        self.runner.execute(run['id'])
        final, receipt = self.repo.inspect(run['id'])
        self.assertEqual(final['state'], 'COMPLETE')
        self.assertEqual(receipt['sha256'], artifact['sha256'])
        self.assertEqual(self.backend.renders, ['source-1'])
        self.assertEqual(len(list(self.output.glob('*.xlsx'))), 1)

    def test_changed_published_file_is_preserved_and_frozen_source_can_be_retried(self):
        run = self.repo.manual(self.plan, 'request-B')
        with patch.object(self.repo, 'confirmed', side_effect=OSError('回执写入中断')):
            self.runner.execute(run['id'])
        _, artifact = self.repo.inspect(run['id'])
        file = Path(artifact['path'])
        file.write_text('业务人员改稿', encoding='utf-8')
        self.repo.request(run['id'])
        self.runner.execute(run['id'])
        self.assertEqual(file.read_text(encoding='utf-8'), '业务人员改稿')
        self.assertEqual(self.repo.inspect(run['id'])[0]['state'], 'REVIEW')
        file.rename(file.with_stem('业务原件'))
        self.repo.request(run['id'])
        self.backend.next_version = 'source-2'
        self.runner.execute(run['id'])
        self.assertEqual(self.backend.renders, ['source-1', 'source-1'])

    def test_do_not_replay_missed_trigger_or_resume_migrated_plan(self):
        self.repo.save_plan(self.plan)
        self.clock += timedelta(hours=1)
        run, = self.repo.due()
        self.assertEqual(run['state'], 'MISSED')
        active = self.repo.manual(self.plan, 'migration')
        from order_migration import pause_imported
        pause_imported(self.root)
        self.assertFalse(self.repo.specs()[0]['enabled'])
        self.assertEqual(self.repo.inspect(active['id'])[0]['state'], 'PAUSED')
        self.assertIsNone(self.repo.next())
