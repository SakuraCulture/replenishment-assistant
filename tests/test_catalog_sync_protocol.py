import os
import json
import subprocess
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from catalog_sync_client import Client, ComponentError, components


@unittest.skipUnless((components()[1]/'runtime/node.exe').exists(),'locked Node runtime unavailable')
class CatalogProtocolTests(unittest.TestCase):
    def test_busy_lock_is_business_readable_then_original_service_releases_it_without_reboot(self):
        with tempfile.TemporaryDirectory() as folder:
            original,other=Client(folder),Client(folder)
            try:
                original.start();before=(Path(folder)/'jobs.json').read_bytes()
                with self.assertRaises(ComponentError) as failed:other.start()
                self.assertEqual(failed.exception.code,'CATALOG_STATE_BUSY');self.assertEqual(failed.exception.details['owner_pid'],original.process.pid)
                self.assertIn('进程',str(failed.exception));self.assertEqual((Path(folder)/'jobs.json').read_bytes(),before)
            finally:self.assertTrue(other.close());self.assertTrue(original.close())
            recovered=Client(folder)
            try:
                recovered.start();self.assertEqual((Path(folder)/'jobs.json').read_bytes(),before)
                self.assertEqual(recovered.request('sync.status')['batches'],[])
            finally:self.assertTrue(recovered.close())

    def test_parent_watch_releases_real_child_even_while_its_stdin_pipe_remains_open(self):
        with tempfile.TemporaryDirectory() as folder:
            sentinel=subprocess.Popen([sys.executable,'-c','import sys; sys.stdin.buffer.read()'],stdin=subprocess.PIPE,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            child=Client(folder)
            try:
                with patch('catalog_sync_client.os.getpid',return_value=sentinel.pid):child.start()
                self.assertTrue(child.running);sentinel.stdin.close();sentinel.wait(timeout=5)
                child.process.wait(timeout=8);self.assertEqual(child.process.returncode,0)
                self.assertFalse(child.process.stdin.closed);self.assertFalse((Path(folder)/'service-owner.json').exists())
            finally:
                if sentinel.poll() is None:sentinel.stdin.close();sentinel.wait(timeout=5)
                self.assertTrue(child.close())
            reopened=Client(folder)
            try:reopened.start();self.assertFalse(reopened.request('sync.status')['running'])
            finally:self.assertTrue(reopened.close())

    def test_real_node_child_status_error_retry_shutdown_isolated(self):
        with tempfile.TemporaryDirectory() as folder:
            c=Client(folder)
            try:
                capabilities=c.start();self.assertEqual(capabilities['protocol'],'jst.catalog.v1')
                self.assertFalse(capabilities['password_login'])
                with self.assertRaises(ComponentError):c.request('submitOrderImport')
                self.assertFalse(c.request('sync.status')['running'])
                self.assertEqual(c.request('session.release'),{'idle':True})
                self.assertTrue(c.running)
                self.assertEqual(c.request('sync.status')['connections'],[])
                op=c.request('accounts.probe',warehouse='山东仓',profilePath=str(Path(folder)/'missing'/'Default'))['operation_id']
                until=time.monotonic()+5
                while True:
                    state=c.request('sync.status')
                    if state['operations'][op]['state']!='RUNNING':break
                    self.assertLess(time.monotonic(),until);time.sleep(.01)
                self.assertEqual(state['operations'][op]['state'],'FAILED')
                self.assertEqual(c.request('sync.stop')['stop_requested'],True)
                self.assertEqual(c.request('sync.status')['accounts'],[])
            finally:self.assertTrue(c.close())


if __name__=='__main__':unittest.main()
