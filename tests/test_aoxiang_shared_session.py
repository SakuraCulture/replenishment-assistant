"""Shared transport ownership without Qt, browser, platform writes or user state."""
import sys
import threading
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from aoxiang_session import SessionOwner, SessionGate


class Client:
    def __init__(self):
        self.running = False
        self.starts = self.closes = 0
        self.idle = True
        self.actions = []
        self.can_close = True
    def start(self): self.running = True;self.starts += 1
    def close(self, timeout=.5):
        self.closes += 1
        if self.can_close:self.running = False
        return self.can_close
    def request(self, action, **kw):
        self.actions.append(action)
        if action == 'session.release':return {'idle':self.idle}
        return {'action':action}


class SharedSessionTests(unittest.TestCase):
    def setUp(self):
        self.created = []
        def factory():
            c = Client();self.created.append(c);return c
        self.owner = SessionOwner(factory)
        self.gate = SessionGate()
    def lease(self): return self.owner.lease(self.gate.request_foreground())
    def test_setup_advice_purchase_receipt_and_queue_reuse_one_host(self):
        for action in ['setup.start', 'configuration', 'purchase.list', 'receipt.list']:
            lease = self.lease();lease.start();lease.request(action)
            self.assertTrue(lease.close());self.assertFalse(lease.running)
            self.assertTrue(self.owner.client.running)
            with self.assertRaises(RuntimeError):lease.request('purchase.start')
        token = self.gate.claim_background();background = self.owner.lease();background.start()
        background.request('receipt.list');self.assertTrue(background.close());self.gate.release_background(token)
        self.assertEqual(len(self.created), 1);self.assertEqual(self.created[0].starts, 1)
        self.assertEqual(self.created[0].closes, 0)
        self.assertTrue(self.owner.close());self.assertEqual(self.created[0].closes, 1)
    def test_handoff_waits_for_async_action_and_never_replays_it(self):
        token = self.gate.claim_background();background = self.owner.lease();background.start()
        c = self.owner.client;c.idle = False;background.request('purchase.start')
        front = self.lease();self.assertTrue(token.is_set())
        passed = threading.Event();t = threading.Thread(target=lambda:(front.start(),passed.set()));t.start()
        self.assertFalse(background.close());self.assertFalse(passed.wait(.05))
        c.idle = True;self.assertTrue(background.close());self.gate.release_background(token)
        self.assertTrue(passed.wait(1));t.join();front.close()
        self.assertEqual(c.actions.count('purchase.start'),1);self.assertEqual(c.closes,0)
    def test_cancel_before_acquiring_does_not_start_a_host(self):
        lease = self.owner.lease(self.gate.request_foreground(),lambda:True)
        with self.assertRaises(RuntimeError):lease.start()
        self.assertTrue(lease.close());self.assertEqual(self.created,[])
    def test_explicit_reset_waits_for_old_process_then_starts_once(self):
        lease = self.lease();lease.start();old = self.owner.client;old.can_close=False
        self.assertFalse(lease.reset());self.assertIs(self.owner.client,old)
        old.can_close=True;self.assertTrue(lease.reset());lease.start()
        self.assertEqual(len(self.created),2);self.assertFalse(old.running);lease.close()
    def test_unknown_handoff_never_grants_operation_before_exit(self):
        lease = self.lease();lease.start();c=self.owner.client;c.can_close=False
        def unknown(*a,**kw):raise TimeoutError('unknown')
        c.request=unknown
        self.assertFalse(lease.close());self.assertTrue(self.gate.yielding)
        c.can_close=True;self.assertTrue(lease.close());self.assertFalse(self.gate.yielding)
    def test_unacknowledged_start_does_not_spawn_twice(self):
        c=Client()
        def unknown():c.running=True;raise TimeoutError('startup')
        c.start=unknown;self.owner.factory=lambda:c
        with self.assertRaises(TimeoutError):self.owner.start()
        with self.assertRaisesRegex(RuntimeError,'尚未就绪'):self.owner.start()
        self.assertTrue(self.owner.close())


if __name__ == '__main__':unittest.main()
