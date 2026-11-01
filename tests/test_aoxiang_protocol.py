"""Isolated host/Node protocol tests. No browser operations or production business database."""
import copy
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from aoxiang_client import ProcessClient, ComponentError
from aoxiang_jobs import Jobs, fingerprint

COMPONENT = ROOT / 'components/aoxiang'
NODE = COMPONENT / 'runtime/node.exe'


def command():
    return {'protocol_version': 1, 'command_id': 'job-1', 'operation': 'replenishment.export',
            'account_binding': {'account_id': '001', 'merchant_code': 'M'},
            'scope': {'warehouse_codes': ['W2', 'W1']},
            'basis_versions': {'store_mapping': 'mapping-1'}, 'policy': {'freshness': 'reuse'}}


def record(sequence=1, state='QUEUED'):
    return {'id': 'job-1', 'request': command(), 'fingerprint': fingerprint(command()),
            'state': state, 'sequence': sequence, 'items': [{'warehouse_code': 'W2'}, {'warehouse_code': 'W1'}]}


class HostJobsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.jobs = Jobs(self.root / 'jobs.sqlite')
        self.jobs.register(command(), {'batch_id': 'b1', 'store_mapping': 'mapping-1'})

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_intent_and_source_cannot_be_changed(self):
        self.jobs.register(command(), {'batch_id': 'b1', 'store_mapping': 'mapping-1'})
        changed = command()
        changed['scope']['warehouse_codes'].reverse()
        with self.assertRaisesRegex(ValueError, '不能更换'):
            self.jobs.register(changed, {'batch_id': 'b1', 'store_mapping': 'mapping-1'})
        with self.assertRaisesRegex(ValueError, '不能更换'):
            self.jobs.register(command(), {'batch_id': 'different'})
        self.assertEqual(self.jobs.get('job-1')['command'], command())

    def test_old_receipt_does_not_replace_newer_job_or_order(self):
        self.assertTrue(self.jobs.observe('job-1', record(5, 'SUCCEEDED')))
        self.assertFalse(self.jobs.observe('job-1', record(2, 'RUNNING')))
        self.assertEqual(self.jobs.get('job-1')['state'], 'SUCCEEDED')
        with self.assertRaisesRegex(ValueError, '相同回执'):
            self.jobs.observe('job-1', record(5, 'RUNNING'))
        wrong = record(6)
        wrong['request']['scope']['warehouse_codes'].reverse()
        with self.assertRaisesRegex(ValueError, '不一致'):
            self.jobs.observe('job-1', wrong)
        self.assertEqual(self.jobs.get('job-1')['sequence'], 5)

    def test_lost_ack_keeps_intent_reconnect_queries_original(self):
        jobs = self.jobs

        class FakeClient:
            def request(self, action, **values):
                # Assert the source exists before any send takes place.
                self.saved = jobs.get('job-1')
                if action == 'reserve':
                    raise ComponentError('RESPONSE_UNKNOWN', '模拟响应丢失')
                return record(3, 'PAUSED')

        client = FakeClient()
        with self.assertRaises(ComponentError):
            self.jobs.dispatch(client, 'job-1')
        self.assertEqual(client.saved['command'], command())
        self.assertTrue(self.jobs.get('job-1')['last_error']['requires_query'])
        reopened = Jobs(self.root / 'jobs.sqlite')
        self.assertEqual(reopened.refresh(client, 'job-1')['state'], 'PAUSED')
        self.assertIsNone(reopened.get('job-1')['last_error'])

    def test_missing_database_or_tables_fail_closed(self):
        with closing(sqlite3.connect(self.jobs.path)) as db, db:
            db.execute('DROP TABLE ax_jobs')
        with self.assertRaisesRegex(ValueError, '不能重建'):
            Jobs(self.jobs.path)
        self.jobs.path.unlink()
        with self.assertRaisesRegex(ValueError, '已丢失'):
            Jobs(self.jobs.path)


@unittest.skipUnless(NODE.is_file(), '需要AX01校验安装的固定Node运行时')
class ActualProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            self.assertTrue(client.close(timeout=10))
        self.temp.cleanup()

    def connect(self, directory='state'):
        client = ProcessClient(COMPONENT, self.root / directory)
        self.clients.append(client)
        return client

    def test_real_node_query_error_shutdown_and_session_exclusion(self):
        client = self.connect()
        ready = client.start(timeout=15)
        self.assertFalse(ready['configured'])
        self.assertEqual(ready['operations'], ['replenishment.export'])
        self.assertEqual(client.request('list')['items'], [])
        with self.assertRaises(ComponentError) as missing:
            client.request('status', command_id='missing')
        self.assertEqual(missing.exception.code, 'COMMAND_NOT_FOUND')
        other = self.connect()
        with self.assertRaises(ComponentError) as busy:
            other.start(timeout=15)
        self.assertEqual(busy.exception.code, 'APP_BUSY')
        self.assertTrue(other.close())
        self.assertTrue(client.close())
        resumed = self.connect()
        self.assertEqual(resumed.start()['recovered'], [])
        self.assertIsNone(resumed.request('ping')['active_command_id'])

    def test_host_fingerprint_is_identical_to_node_contract(self):
        script = "import {commandFingerprint} from './src/machine-commands.mjs'; process.stdout.write(commandFingerprint(JSON.parse(process.argv[1])));"
        result = subprocess.run([str(NODE), '--input-type=module', '-e', script, json.dumps(command(), ensure_ascii=False)],
                                cwd=COMPONENT, capture_output=True, check=True)
        self.assertEqual(result.stdout.decode('ascii'), fingerprint(command()))

    def test_real_dispatch_reopen_and_original_id_binding(self):
        # Seed a synthetic directory, with no browser binding: execution must stop before browser I/O.
        state_root = self.root / 'seeded'
        script = """import {StateStore} from './src/state.mjs';
import {freshConfig,saveConfig} from './src/config.mjs';
const root=process.argv[1],state=await StateStore.open(root);
await saveConfig(root,freshConfig({accountId:'001',merchantCode:'M',displayName:'测试'},['W2','W1'].map(code=>({buyerWarehouseCode:code,displayName:code,operatingStores:[],mappingSource:'fixture',mappingVerifiedAt:'2026-11-01T00:00:00Z'}))));
state.close();"""
        subprocess.run([str(NODE), '--input-type=module', '-e', script, str(state_root)], cwd=COMPONENT, capture_output=True, check=True)
        client = self.connect('seeded')
        client.start()
        config = client.request('configuration')
        cmd = command()
        cmd['basis_versions']['store_mapping'] = config['store_mapping_version']
        jobs = Jobs(self.root / 'host.sqlite')
        jobs.register(cmd, {'batch_id': 'synthetic-batch'})
        accepted = client.request('reserve', command=cmd)  # Simulate host crashing before storing this response.
        ids = [i['run_id'] for i in accepted['record']['items']]
        until = time.monotonic() + 5
        while time.monotonic() < until:
            observed = jobs.refresh(client, 'job-1')
            if observed['state'] == 'INTERRUPTED':
                break
            time.sleep(.01)
        self.assertEqual(observed['state'], 'INTERRUPTED')
        self.assertEqual(observed['record']['error']['code'], 'SETUP_REQUIRED')
        replay = jobs.dispatch(client, 'job-1')
        self.assertEqual([i['run_id'] for i in replay['record']['items']], ids)
        self.assertEqual(client.request('list')['total'], 1)
        self.assertTrue(client.close())
        reopened = self.connect('seeded')
        reopened.start()
        recovered = Jobs(self.root / 'host.sqlite').refresh(reopened, 'job-1')
        self.assertEqual([i['run_id'] for i in recovered['record']['items']], ids)

    def test_late_response_cannot_satisfy_new_request(self):
        # Synthetic protocol peer tests transport correlation; no platform operations.
        peer = self.root / 'peer.py'
        peer.write_text('''import json,sys,time,threading
lock=threading.Lock()
def send(v):
 with lock: print(json.dumps(dict(protocol_version=1,**v)),flush=True)
def answer(r):
 if r['action']=='slow': time.sleep(.25)
 send(dict(type='response',request_id=r['request_id'],ok=True,result=r['action']))
send(dict(type='ready'))
threads=[]
for line in sys.stdin:
 r=json.loads(line)
 if r['action']=='shutdown': answer(r);break
 t=threading.Thread(target=answer,args=(r,));t.start();threads.append(t)
for t in threads: t.join()
''', encoding='utf-8')
        original = subprocess.Popen
        def launch(_argv, **kwargs):
            return original([sys.executable, str(peer)], **kwargs)
        client = self.connect('synthetic')
        with patch('aoxiang_client.subprocess.Popen', side_effect=launch):
            client.start()
        with self.assertRaises(ComponentError) as timeout:
            client.request('slow', timeout=.03)
        self.assertEqual(timeout.exception.code, 'RESPONSE_UNKNOWN')
        self.assertEqual(client.request('ping'), 'ping')
        time.sleep(.3)
        self.assertEqual(client.request('list'), 'list')


if __name__ == '__main__':
    unittest.main()
