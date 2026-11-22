"""The new payment handoff and filtered selection share the original procurement queue."""

import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from aoxiang_preview_scope import enqueue, existing_tasks, inspect_scope, payment_heads
from aoxiang_workflow_runner import Runner
from aoxiang_workflows import Workflows
import daily_batch
import lifecycle_recovery
import order_lifecycle as lifecycle
import work_repository as wr
import test_lifecycle as fixtures
from test_lifecycle_filter_explanation import seed


class PreviewHandoff(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.LifecycleTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.path = self.f.path
        self.store = Workflows(self.path.parent/'aoxiang', business_path=self.path)
        self.f.save('return')

    def saved(self, amount=15):
        return self.f.save('payment', [{'付款单商品数量': 6, '实际金额': amount}])[1]

    def test_receipt_contains_only_exact_payment_warehouse_and_survives_recovery(self):
        receipt = self.saved()
        hb, = payment_heads(receipt)
        self.assertEqual(hb['warehouse'], '河北仓')
        self.assertEqual(hb['oid'], self.f.h['oid'])
        self.assertNotIn('products', hb)
        current, = inspect_scope(self.path, self.store.folder, [hb])
        self.assertEqual(current['state'], 'ready')
        self.assertFalse(self.store.path.exists())
        saved_task = lifecycle_recovery.recent(self.path, stage='payment')[0]
        self.assertEqual(payment_heads(lifecycle_recovery.inspect(self.path, saved_task['id'])['receipt']), [hb])
        self.f.h = self.f.heads[1]
        sd = self.f.save('payment', [{'商品编码': '0002', '供货仓': '山东仓'}])[1]
        rows = inspect_scope(self.path, self.store.folder, payment_heads(receipt+sd))
        self.assertEqual([r['state'] for r in rows], ['ready','blocked'])
        self.assertIn('尚未确认', rows[-1]['message'])

    def test_original_batch_reused_after_preview_and_unknown_acceptance(self):
        heads = payment_heads(self.saved())
        prepared = enqueue(self.store, heads)
        item, = prepared['items']
        self.assertEqual(item['expected_payment'], heads[0]['preview_payment'])
        self.store.update(prepared['id'], position=0, changes={'state':'PREPARED','package':{'id':'original-p'}})
        ready = self.store.update(prepared['id'], state='READY')
        state, = inspect_scope(self.path, self.store.folder, heads)
        self.assertEqual((state['state'], state['batch']), ('original', prepared['id']))
        upload = self.store.upload(prepared['id'], [0], ready['sequence'])
        self.store.update(upload['id'], position=0, changes={'state':'REVIEW',
            'purchase_observation': {'uploadAttempted': True}, 'error':{'code':'SUBMISSION_UNKNOWN'}})
        self.store.update(upload['id'], state='COMPLETED_WITH_ERRORS')
        state, = inspect_scope(self.path, self.store.folder, heads)
        self.assertEqual((state['state'], state['batch']), ('original', upload['id']))
        with self.assertRaisesRegex(ValueError,'范围已变化'):
            enqueue(self.store, heads)
        with self.assertRaisesRegex(ValueError,'原采购任务'):
            self.store.prepare(heads, payments={heads[0]['number']:heads[0]['preview_payment']})
        self.assertEqual(len(self.store.list()), 2)

    def test_component_unknown_without_host_batch_blocks_new_preview(self):
        heads = payment_heads(self.saved())
        head, = heads
        path = self.store.folder/'component-state'/'state.sqlite'
        path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE purchase_creates(id,state,platform_order,payload)')
            payload = {'intent': {'basis': {k:head[k] for k in ('oid','number','warehouse','store')}},
                       'uploadAttempted': True}
            db.execute('INSERT INTO purchase_creates VALUES(?,?,?,?)', ('unknown','NEEDS_REVIEW',None,json.dumps(payload)))
        state, = inspect_scope(self.path, self.store.folder, heads)
        self.assertEqual((state['state'], state['batch'], state['purchase_id']), ('original',None,'unknown'))
        with self.assertRaisesRegex(ValueError,'范围已变化'):
            enqueue(self.store, heads)
        self.assertFalse(self.store.path.exists())

    def test_changed_payment_between_enqueuing_and_processing_is_blocked_before_component(self):
        heads = payment_heads(self.saved())
        job = enqueue(self.store, heads)
        self.saved(amount=18)
        from threading import Event
        class NoComponent:
            def request(self, *args, **kwargs):
                raise AssertionError('Preflight reached the component before rejecting the stale version')
        result = Runner(self.store, NoComponent(), self.store.folder, self.path, cancel=Event()).run(job['id'])
        self.assertEqual(result['state'], 'COMPLETED_WITH_ERRORS')
        self.assertEqual(result['items'][0]['error']['code'], 'PAYMENT_SELECTION_CHANGED')

    def test_changed_payment_before_enqueuing_does_not_create_batch(self):
        heads = payment_heads(self.saved())
        self.saved(amount=18)
        check, = inspect_scope(self.path, self.store.folder, heads)
        self.assertIn('付款版本已变化', check['message'])
        with self.assertRaisesRegex(ValueError,'范围已变化'):
            enqueue(self.store, heads)
        self.assertFalse(self.store.path.exists())


class PurchaseFilter(unittest.TestCase):
    def test_filtered_scope_covers_multiple_pages_and_limits_new_work(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            work, _ = seed(root, total=260)
            cache = root/'list-cache.sqlite'
            with closing(work.connect()) as db, db:
                for i in range(191):
                    doc = {'schema':1,'files':[],'rows':[{'sku':'0001','name':'合成商品','base_quantity':'0' if i==190 else '4'}]}
                    db.execute('INSERT INTO lifecycle_versions VALUES(?,?,?,?,?,?,?,?)',
                        (f'synthetic-{i:03d}',1,'河北仓','payment',1,wr.now(),lifecycle.fingerprint(doc),wr.dumps(doc)))
            request = {'from': wr.today(), 'to': wr.today(), 'stage_view': 'payment',
                       'stage_filter': daily_batch.PURCHASE_FILTER, 'explain_filters': True}
            first = daily_batch.read(work.path, cache, request)
            self.assertEqual((first['count'],len(first['records']),len(first['purchase_scope'])), (190,50,190))
            store = Workflows(work.path.parent/'aoxiang', business_path=work.path)
            original = store.prepare([first['records'][0]])
            fourth = daily_batch.read(work.path, cache, {**request,'page':3})
            self.assertEqual((fourth['count'], len(fourth['records'])), (189, 39))
            self.assertEqual(fourth['records'][-1]['number'], 'BH-S189-HB')
            tasks = existing_tasks(work.path, store.folder, [first['records'][0]])
            self.assertEqual(next(iter(tasks.values()))['batch'], original['id'])
            reasons = {r['store']:r['reasons'] for r in fourth['filter_explanation']['excluded']}
            self.assertTrue(any('原采购批次' in x for x in reasons['S000']))
            self.assertIn('正式付款数量为零', reasons['S190'])
            self.assertIn('尚无有效正式付款', reasons['S191'])
            only = daily_batch.read(work.path, cache, {**request, 'search':'S189', 'warehouse':'河北仓',
                                    'scope':[['synthetic-188','河北仓'],['synthetic-189','河北仓']]})
            self.assertEqual([r['number'] for r in only['purchase_scope']], ['BH-S189-HB'])
            self.assertFalse(daily_batch.read(work.path, cache, {**request, 'warehouse':'山东仓'})['records'])
            with closing(work.connect()) as db, db:
                for i in range(192, 260):
                    doc = {'schema':1,'files':[],'rows':[{'sku':'0001','name':'合成商品','base_quantity':'4'}]}
                    db.execute('INSERT INTO lifecycle_versions VALUES(?,?,?,?,?,?,?,?)',
                        (f'synthetic-{i:03d}',1,'河北仓','payment',1,wr.now(),lifecycle.fingerprint(doc),wr.dumps(doc)))
            over = daily_batch.read(work.path, cache, request)
            self.assertEqual(over['count'], 257)
            self.assertIsNone(over['purchase_scope'])
