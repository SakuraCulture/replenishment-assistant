import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from tests.test_catalog_session import Client, SessionOwner
from catalog_sync_client import ComponentError
from unattended_resources import GIB, Resources, configure


class ScopedClient(Client):
    def start(self):
        super().start()
        return {'warehouse_slots': True}


class ResourceTests(unittest.TestCase):
    def test_pressure_bounds_local_work_and_recovers_gradually(self):
        with TemporaryDirectory() as temp:
            at = [0]
            budget = SimpleNamespace(headroom=8*GIB, mode='ample', refresh=lambda: None)
            resources = Resources(temp, budget=budget, clock=lambda: at[0], system=lambda: {
                'cpu_idle_ticks': int(at[0]*8), 'cpu_total_ticks': int(at[0]*10)})
            self.assertEqual(resources.warehouse_slots(), 1)
            configure(temp, True)
            resources.check()
            at[0] = 1
            self.assertEqual(resources.warehouse_slots(), 2)
            self.assertTrue(resources.try_heavy(self, temp))
            second = object()
            self.assertFalse(resources.try_heavy(second, temp))
            at[0] = 2
            budget.headroom = 512*1024**2
            self.assertEqual(resources.check(), 'protected')
            resources.release_heavy(self)
            self.assertFalse(resources.try_heavy(second, temp))
            budget.headroom = 8*GIB
            for second_mark, expected in ((3, 'protected'), (62, 'protected'), (63, 'normal'),
                                          (93, 'normal'), (123, 'normal')):
                at[0] = second_mark
                self.assertEqual(resources.check(), expected)
                self.assertEqual(resources.warehouse_slots(), 2 if second_mark == 123 else 1)
            self.assertTrue(resources.try_heavy(second, temp))

    def test_wait_for_same_warehouse_and_drain_unknown_without_stopping_sibling(self):
        with TemporaryDirectory() as temp:
            owner = SessionOwner(Path(temp), ScopedClient, SimpleNamespace(warehouse_slots=lambda: 2))
            sd = owner.acquire(['山东仓']); sd.start()
            hb = owner.acquire(['河北仓']); hb.start()
            client = owner.client
            self.assertEqual(len(owner.leases), 2)
            with self.assertRaisesRegex(ComponentError, '在使用中'):
                owner.acquire(['山东仓'])
            arrived, waiting = [], threading.Event()
            thread = threading.Thread(target=lambda: arrived.append(owner.acquire_wait(['山东仓'], threading.Event(), lambda _:waiting.set())))
            thread.start()
            self.assertTrue(waiting.wait(2))
            self.assertTrue(sd.close())
            thread.join(3)
            self.assertEqual(len(arrived), 1)
            other = arrived[0]; other.start()
            scope = client.actions[0][1]
            self.assertEqual((scope['lease_id'], scope['scope']), (sd.id, ['山东仓']))
            self.assertTrue(hb.running)
            client.error = TimeoutError('controlled transport interruption')
            self.assertFalse(hb.close())
            self.assertEqual(client.closes, 0)
            self.assertTrue(other.close())
            self.assertTrue(hb.close())
            self.assertEqual(client.closes, 1)


if __name__ == '__main__':
    unittest.main()
