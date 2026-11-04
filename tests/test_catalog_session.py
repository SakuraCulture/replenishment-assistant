"""Connection lifetime and exclusive handoff; no browser or user data."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from catalog_sync_session import SessionOwner
from catalog_sync_client import ComponentError


class Client:
    def __init__(self, folder):
        self.running=False;self.error=None;self.starts=0;self.closes=0
        self.can_close=True;self.idle=True;self.actions=[]
    def start(self):self.running=True;self.starts+=1
    def close(self,timeout=.5):
        self.closes+=1
        if self.can_close:self.running=False
        return self.can_close
    def request(self,action,**values):
        self.actions.append((action,values))
        if action=='session.release':return {'idle':self.idle}
        return {'action':action}


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.created=[]
        def factory(folder):
            c=Client(folder);self.created.append(c);return c
        self.owner=SessionOwner('isolated',factory)
    def lease(self):
        lease=self.owner.acquire();lease.start();return lease
    def test_reopen_across_default_and_allocation_windows_reuses_component(self):
        for purpose in ('default_catalog','allocation_baseline','default_catalog'):
            lease=self.lease();lease.request('sync.status');self.assertTrue(lease.close())
            self.assertFalse(lease.running);self.assertTrue(self.owner.client.running)
            with self.assertRaises(ComponentError):lease.request('sync.start')
        self.assertEqual(len(self.created),1);self.assertEqual(self.created[0].starts,1)
        self.assertEqual(self.created[0].closes,0);self.assertTrue(self.owner.close())
        self.assertFalse(self.created[0].running)
    def test_inflight_cancel_retains_exclusive_lease_until_safe(self):
        lease=self.lease();c=self.owner.client;c.idle=False
        self.assertFalse(lease.close())
        with self.assertRaises(ComponentError):self.owner.acquire()
        self.assertEqual(c.closes,0);c.idle=True;self.assertTrue(lease.close())
        self.assertTrue(self.lease().close());self.assertEqual(len(self.created),1)
    def test_unknown_release_waits_for_original_process_exit(self):
        lease=self.lease();c=self.owner.client;c.can_close=False
        c.request=lambda *a,**kw:(_ for _ in ()).throw(TimeoutError('lost reply'))
        self.assertFalse(lease.close())
        with self.assertRaises(ComponentError):self.owner.acquire()
        c.can_close=True;self.assertTrue(lease.close());self.assertIsNone(self.owner.client)
    def test_unacknowledged_start_never_spawns_second_component(self):
        c=Client('test')
        def start():c.running=True;raise TimeoutError('startup unknown')
        c.start=start;self.owner.factory=lambda _:c;lease=self.owner.acquire()
        with self.assertRaises(TimeoutError):lease.start()
        with self.assertRaises(ComponentError):lease.start()
        c.can_close=False;self.assertFalse(lease.close())
        c.can_close=True;self.assertTrue(lease.close())
    def test_explicit_warehouse_reconnect_does_not_restart_healthy_component(self):
        lease=self.lease();lease.reconnect('河北仓');c=self.owner.client
        self.assertEqual(c.actions,[('session.reset',{'warehouse':'河北仓'})])
        self.assertEqual(c.starts,1);self.assertEqual(c.closes,0);lease.close()
    def test_dead_component_recovery_keeps_tasks_without_replaying(self):
        lease=self.lease();old=self.owner.client;old.running=False;old.error=RuntimeError('exit')
        lease.reconnect('山东仓');self.assertEqual(len(self.created),2)
        self.assertEqual(self.created[1].actions,[('session.reset',{'warehouse':'山东仓'})]);lease.close()
    def test_lost_protocol_cannot_restart_before_shutdown_finishes(self):
        lease=self.lease();c=self.owner.client;c.error=RuntimeError('unknown');c.can_close=False
        with self.assertRaises(ComponentError):lease.reconnect('山东仓')
        self.assertEqual(len(self.created),1)
        c.can_close=True;lease.reconnect('山东仓');self.assertEqual(len(self.created),2);lease.close()
    def test_dead_between_windows_starts_one_new_host(self):
        lease=self.lease();lease.close();old=self.owner.client;old.running=False
        next_lease=self.lease();self.assertEqual(len(self.created),2);next_lease.close()
    def test_unused_window_releases_without_starting_component(self):
        lease=self.owner.acquire();self.assertTrue(lease.close());self.assertEqual(self.created,[])

    def test_download_and_adoption_keep_the_same_absolute_state_directory(self):
        lease=self.lease()
        self.assertEqual(lease.folder,Path('isolated').resolve())
        lease.close()
        reopened=self.lease();self.assertEqual(reopened.folder,lease.folder);reopened.close()


if __name__=='__main__':unittest.main()
