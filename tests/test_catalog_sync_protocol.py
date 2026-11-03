import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from catalog_sync_client import Client, ComponentError, components


@unittest.skipUnless((components()[1]/'runtime/node.exe').exists(),'locked Node runtime unavailable')
class CatalogProtocolTests(unittest.TestCase):
    def test_real_node_child_status_error_retry_shutdown_isolated(self):
        with tempfile.TemporaryDirectory() as folder:
            c=Client(folder)
            try:
                capabilities=c.start();self.assertEqual(capabilities['protocol'],'jst.catalog.v1')
                self.assertFalse(capabilities['password_login'])
                with self.assertRaises(ComponentError):c.request('submitOrderImport')
                self.assertFalse(c.request('sync.status')['running'])
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
