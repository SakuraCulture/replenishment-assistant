"""The background cadence and outstanding originals survive restarts of the workbench."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import jst_fulfillment_jobs as jobs
import jst_fulfillment_round as rounds
from work_repository import Repository


class PaymentRound(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Repository(Path(folder.name)).path
        self.clock = self.enterContext(patch.object(rounds, 'now_ms', return_value=1000))
        self.locks = self.enterContext(patch.object(jobs, 'due_payment_locks', return_value=[]))
        self.orders = self.enterContext(patch.object(jobs, 'due_order_payment', return_value=[]))

    def test_empty_round_restarts_and_late_launches(self):
        deadline, pending = rounds.current(self.path)
        self.assertEqual((deadline, pending), (1000 + 43_200_000, []))
        self.orders.return_value = ['new']
        for instant in (1000 + 30_000, deadline - 1):
            self.clock.return_value = instant
            self.assertEqual(rounds.current(self.path), (deadline, []))
        self.orders.assert_called_once_with(self.path)
        self.clock.return_value = deadline
        self.assertEqual(rounds.current(self.path), (deadline + 43_200_000, [('new', 'payment')]))
        self.clock.return_value += 5 * rounds.INTERVAL_MS
        next_at, _ = rounds.current(self.path)
        self.assertEqual(next_at, self.clock.return_value + rounds.INTERVAL_MS)
        self.assertEqual(self.orders.call_count, 3)

    def test_bounded_round_claims_each_original_only_once(self):
        self.locks.return_value = [f'L{i}' for i in range(5)]
        self.orders.return_value = [f'P{i}' for i in range(10)]
        until, pending = rounds.current(self.path)
        self.assertEqual([k for k, _ in pending], self.locks.return_value + self.orders.return_value[:5])
        self.locks.assert_called_once_with(self.path, limit=5)
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: rounds.take(self.path, until, 'L0', None), range(2)))
        self.assertEqual(sorted(claims), [False, True])
        self.locks.assert_called_with(self.path, online='L0')
        self.orders.return_value = []  # The user handled P0 while the browser was occupied.
        self.assertFalse(rounds.take(self.path, until, 'P0', None))
        self.orders.assert_called_with(self.path, online='P0')
        deadline, remainder = rounds.current(self.path)
        self.assertEqual(deadline, until)
        self.assertEqual(len(remainder), 8)
        self.assertNotIn('P0', dict(remainder))
        self.clock.return_value = until
        self.assertFalse(rounds.take(self.path, until, 'L1', None))

    def test_browser_idle_notification_waits_for_scheduled_work(self):
        from catalog_sync_session import SessionOwner
        notifications = []
        owner = SessionOwner(self.path.parent, on_idle=lambda: notifications.append(True))
        order = owner.acquire_background()
        owner.set_scheduled_queue(True)
        order.close()
        self.assertEqual(notifications, [])
        self.assertIsNone(owner.acquire_background())
        scheduled = owner.acquire(['山东仓'])
        owner.set_scheduled_queue(False)
        self.assertEqual(notifications, [])
        scheduled.close()
        self.assertEqual(notifications, [True])


if __name__ == '__main__':
    unittest.main()
