import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from catalog_sync_client import ComponentError
from catalog_sync_session import SessionOwner
import jst_fulfillment as fulfillment
from runtime_support import Cancelled


class ScheduledBrowserTests(unittest.TestCase):
    def test_waiter_takes_browser_between_orders(self):
        owner = SessionOwner('.')
        entered, waiting, finish = (threading.Event() for _ in range(3))
        seen, messages, result = [], [], []
        def read(path, keys, browser, progress, cancel):
            seen.extend(keys)
            entered.set()
            self.assertTrue(finish.wait(4))
            return list(keys)
        def scheduled():
            lease = owner.acquire_wait(['山东仓'], threading.Event(), lambda _: waiting.set(), scheduled=True)
            result.append('scheduled')
            lease.close()
        with patch('catalog_sync_session.SessionLease.start'), patch.object(fulfillment, 'pull_order_payment', read):
            worker = threading.Thread(target=lambda:result.extend(fulfillment.retry_order_payment(
                None,['one','two','three'],owner,messages.append)))
            worker.start()
            self.assertTrue(entered.wait(3))
            scheduler = threading.Thread(target=scheduled)
            scheduler.start()
            self.assertTrue(waiting.wait(3))
            finish.set()
            worker.join(4)
            scheduler.join(4)
        self.assertEqual(seen,['one'])
        self.assertCountEqual(result,['one','scheduled'])
        self.assertTrue(any('让位' in m for m in messages))
        self.assertFalse(owner.scheduled_waiting)

    def test_cancelled_waiter_releases_priority(self):
        owner, cancel = SessionOwner('.'),threading.Event()
        hold=owner.acquire()
        with self.assertRaises(Cancelled):
            owner.acquire_wait(['河北仓'],cancel,lambda _:cancel.set(),scheduled=True)
        hold.close()
        self.assertIsNotNone(owner.acquire_background())

    def test_queued_serial_schedule_has_priority_before_worker_starts(self):
        owner=SessionOwner('.')
        first=owner.acquire_background()
        owner.set_scheduled_queue(True)
        first.close()
        self.assertIsNone(owner.acquire_background())
        owner.set_scheduled_queue(False)
        self.assertIsNotNone(owner.acquire_background())

    def test_payment_reenters_then_resets_only_original_warehouse(self):
        calls=[]
        class Lease:
            owner=SimpleNamespace(scheduled_waiting=False)
            def request(self,action,**kwargs):
                calls.append((action,kwargs))
                if len(calls)<3:
                    raise ComponentError('JST_PAGE_LOCATION','missing')
                return {'ok':True}
        browser=fulfillment.Browser(Lease(),True)
        self.assertEqual(browser.request('payment_detail',spec={'warehouse':'山东仓'},retained_only=True),{'ok':True})
        self.assertEqual([action for action,_ in calls],['order.payment_detail','order.payment_detail','session.reset','order.payment_detail'])
        self.assertEqual(calls[2][1],{'warehouse':'山东仓'})
        self.assertFalse(calls[-1][1]['retained_only'])
