"""Synthetic allocation-to-review acceptance. Never opens a real browser."""
from contextlib import closing
import copy
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import sqlite3
import unittest

import openpyxl
import jst_auto_order as automatic
import jst_auto_order_store as orders
import jst_fulfillment as service
import jst_fulfillment_store as sources
import order_lifecycle as lifecycle
import payment_match_index
from payment_evidence import mapping_key
import work_repository as wr
import test_jst_auto_order as fixtures


HEADERS = ['内部订单号', '线上订单号', '店铺名称', '商品编码', '商品名称', '实配数量', '商品金额', '商品单价', '数量单位']


class Fulfillment(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AutoOrders()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.path = self.fixture.path
        self.keys = self.fixture.prepared()
        automatic.process(self.path, self.keys, self.fixture.portal, 'preview')
        automatic.process(self.path, self.keys, self.fixture.portal, 'submit')
        self.fixture.portal.orders[self.keys[0]].update(status='audited', native_status='已客审待财审')
        automatic.process(self.path, self.keys, self.fixture.portal, 'recover')
        self.order = self.fixture.read(self.keys[0])
        self.head = self.order['payload']['head']
        self.root = self.fixture.f.f.root

    def original(self, *, qty='12', price='2.5', amount='30', change=None, headers=None, rows=None):
        row = [self.order['internal'], self.order['online'], self.order['payload']['shop']['name'],
               self.order['payload']['lines'][0][0], '合成商品', qty, amount, price, '库存单位']
        if change:
            for index, value in change.items():
                row[index] = value
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = '实配明细'
        sheet.append(headers or HEADERS)
        for value in rows if rows is not None else [row]:
            sheet.append(value)
        out = BytesIO()
        book.save(out)
        book.close()
        raw = out.getvalue()
        receipt = {'binding': self.order['proof']['binding'], 'online': self.order['online'],
                   'internal': self.order['internal'], 'native_status': '已发货', 'complete': True,
                   'sha': sha256(raw).hexdigest(), 'state': 'FILE_READY'}
        return raw, receipt

    def archive(self, **options):
        raw, receipt = self.original(**options)
        payload = service.parse(raw, self.order, receipt)
        with closing(orders.connection(self.path)) as db, db:
            digest = sources.save(db, self.order['online'], payload, raw)
        return payload, digest

    def test_partial_allocation_prepares_review_without_marking_paid(self):
        payload, _ = self.archive()
        plan, overview = service.prepare_review(self.path, self.keys)
        self.assertFalse(lifecycle.snapshot(self.path, self.head)['present']['payment'])
        row = plan['plans'][0]['payload']['raw_rows'][0]
        self.assertEqual((row['base_quantity'], Decimal(row['amount']), row['document_number']), ('12', Decimal('30'), self.order['internal']))
        self.assertEqual(overview[0]['quantity'], '12')
        self.assertEqual(payload['quantity'], '12')

    def test_manual_payment_save_is_idempotent_and_keeps_original(self):
        self.archive()
        plan, _ = service.prepare_review(self.path, self.keys)
        self.assertTrue(lifecycle.commit(self.path, plan)[0]['changed'])
        repeated, _ = service.prepare_review(self.path, self.keys)
        self.assertFalse(lifecycle.commit(self.path, repeated)[0]['changed'])
        self.assertEqual(len(lifecycle.history(self.path, self.head, 'payment')), 1)

    def test_plain_order_quantity_does_not_claim_to_be_allocation(self):
        raw, receipt = self.original(headers=[h.replace('实配数量', '数量') for h in HEADERS])
        with self.assertRaisesRegex(ValueError, '普通.*数量'):
            service.parse(raw, self.order, receipt)

    def test_wrong_company_shop_internal_and_online_are_rejected(self):
        raw, receipt = self.original()
        for key in ('coid', 'uid', 'shop_id', 'warehouse'):
            bad = copy.deepcopy(receipt)
            bad['binding'][key] = 'other'
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, '来源'):
                service.parse(raw, self.order, bad)
        for index in (0, 1, 2):
            bad_raw, bad_receipt = self.original(change={index: 'another'})
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, '其他订单|其他.*店铺'):
                service.parse(bad_raw, self.order, bad_receipt)

    def test_unfinished_cancelled_or_incomplete_is_not_zero(self):
        raw, receipt = self.original()
        for state in ('已取消', '发货中(打单拣货)', '已客审待财审'):
            with self.subTest(state=state), self.assertRaisesRegex(ValueError, '完整实配'):
                service.parse(raw, self.order, {**receipt, 'native_status': state})
        with self.assertRaises(ValueError):
            service.parse(raw, self.order, {**receipt, 'complete': False})

    def test_blank_nan_formula_unit_and_amount_mismatch_stop_entire_order(self):
        for options in ({'qty': None}, {'qty': '=1+1'}, {'qty': 'NaN'}, {'qty': '-1'},
                        {'amount': None}, {'amount': '31'}, {'change': {8: '箱'}}, {'change': {3: 'OTHER'}}):
            raw, receipt = self.original(**options)
            with self.subTest(options=options), self.assertRaises(ValueError):
                service.parse(raw, self.order, receipt)

    def test_explicit_zero_is_retained_without_inventing_missing_zero(self):
        payload, _ = self.archive(qty='0', amount='0')
        self.assertEqual(payload['rows'][0]['quantity'], '0')
        raw, receipt = self.original(rows=[])
        with self.assertRaisesRegex(ValueError, '缺少原单|没有明细'):
            service.parse(raw, self.order, receipt)

    def test_duplicate_sku_and_hash_change_are_rejected(self):
        raw, receipt = self.original()
        with self.assertRaises(ValueError):
            service.parse(raw + b'changed', self.order, receipt)
        book = openpyxl.load_workbook(BytesIO(raw))
        sheet = book.active
        sheet.append([c.value for c in sheet[2]])
        out = BytesIO()
        book.save(out)
        book.close()
        raw = out.getvalue()
        with self.assertRaisesRegex(ValueError, '重复'):
            service.parse(raw, self.order, {**receipt, 'sha': sha256(raw).hexdigest()})

    def test_repeated_source_is_one_candidate_and_recovers_missing_cache(self):
        payload, digest = self.archive()
        with closing(orders.connection(self.path)) as db, db:
            _, raw = sources.source(db, self.order['online'], digest)
            self.assertEqual(sources.save(db, self.order['online'], payload, raw), digest)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM jst_fulfillment_sources').fetchone()[0], 1)
        plan, _ = service.prepare_review(self.path, self.keys)
        from pathlib import Path
        file = Path(plan['files'][0]['path'])
        self.assertTrue(file.is_relative_to(self.root))
        file.unlink()
        again, _ = service.prepare_review(self.path, self.keys)
        self.assertEqual(sha256(file.read_bytes()).hexdigest(), again['files'][0]['sha'])

    def test_cancellation_or_new_source_after_review_blocks_manual_save(self):
        self.archive()
        plan, _ = service.prepare_review(self.path, self.keys)
        with closing(orders.connection(self.path)) as db, db:
            sources.observe(db, self.order['online'], 'CANCELLED', '合成取消')
        with self.assertRaisesRegex(ValueError, '状态变化'):
            lifecycle.commit(self.path, plan)
        self.assertFalse(lifecycle.snapshot(self.path, self.head)['present']['payment'])
        self.archive(qty='6', amount='15')
        with self.assertRaisesRegex(ValueError, '实配资料已更新'):
            lifecycle.commit(self.path, plan)

    def test_removing_guard_cannot_save_allocated_payment(self):
        self.archive()
        plan, _ = service.prepare_review(self.path, self.keys)
        del plan['jst_fulfillment']
        with self.assertRaisesRegex(ValueError, '证据'):
            lifecycle.commit(self.path, plan)

    def test_tampered_archive_and_review_amount_do_not_save(self):
        _, digest = self.archive()
        plan, _ = service.prepare_review(self.path, self.keys)
        plan['plans'][0]['payload']['raw_rows'][0]['amount'] = '1'
        with closing(orders.connection(self.path)) as db, self.assertRaises(ValueError):
            service.validate_guard(db, plan)
        with closing(orders.connection(self.path)) as db, db:
            db.execute('UPDATE jst_fulfillment_sources SET original=? WHERE digest=?', (b'corrupt', digest))
        with self.assertRaisesRegex(ValueError, '原件'):
            service.prepare_review(self.path, self.keys)

    def test_cancelled_pull_records_state_without_payment_or_import(self):
        calls = []
        order = self.order
        class Browser:
            def action(self, action, current, **kw):
                calls.append(action)
                return {'binding': order['proof']['binding'], 'online': order['online'], 'internal': order['internal'],
                        'state': 'CANCELLED', 'message': '原单取消'}
        result = service.pull(self.path, self.keys, Browser())
        self.assertEqual(calls, ['allocation'])
        self.assertEqual(result[0]['state'], 'CANCELLED')
        self.assertFalse(lifecycle.snapshot(self.path, self.head)['present']['payment'])

    def test_native_file_pull_is_ready_but_still_unpaid(self):
        raw, receipt = self.original()
        file = self.root / 'native.xlsx'
        file.write_bytes(raw)
        class Browser:
            def action(self, action, current, **kw):
                return {**receipt, 'path': str(file)}
        result = service.pull(self.path, self.keys, Browser())
        self.assertEqual(result[0]['state'], 'READY')
        self.assertFalse(lifecycle.snapshot(self.path, self.head)['present']['payment'])

    def test_auto_order_is_visible_to_payment_recognition_before_confirmation(self):
        with closing(wr.read_connection(self.path)) as db:
            mappings = payment_match_index.mappings(db)
        binding = self.order['proof']['binding']
        key = mapping_key('order', binding['coid'], self.order['internal'])
        self.assertEqual(mappings[key]['head']['number'], self.head['number'])
        self.assertEqual(mappings[key]['generation_id'], self.order['gid'])
        self.assertEqual(mappings[key]['source'], 'jst_auto_order')
        self.assertFalse(any(k.startswith('payment_match.store.') for k in mappings))

    def test_conflicting_old_payment_mapping_cannot_rebind(self):
        binding = self.order['proof']['binding']
        key = mapping_key('order', binding['coid'], self.order['internal'])
        with closing(orders.connection(self.path)) as db, db:
            db.execute("INSERT INTO lifecycle_mappings VALUES('payment',?,?)", (key, wr.dumps({
                'head': {**self.head, 'number': 'another'}, 'original_name': binding['shop'], 'generation_id': self.order['gid']})))
        with closing(wr.read_connection(self.path)) as db, self.assertRaisesRegex(ValueError, '关联冲突'):
            payment_match_index.mappings(db)

    def test_polling_is_opt_in_bounded_and_never_polls_ready_or_paid_sources(self):
        from datetime import datetime, timedelta
        import jst_fulfillment_jobs as jobs
        self.assertFalse(jobs.enabled(self.path))
        self.assertTrue(jobs.configure(self.path, True))
        self.assertTrue(jobs.enabled(self.path))
        self.assertEqual(jobs.due(self.path), self.keys)
        with closing(orders.connection(self.path)) as db, db:
            sources.observe(db, self.order['online'], 'WAIT_ALLOCATION', '等待配货')
        now = datetime.fromisoformat(wr.now())
        self.assertEqual(jobs.due(self.path, now=now), [])
        self.assertEqual(jobs.due(self.path, now=now + timedelta(minutes=6)), self.keys)
        self.archive()
        self.assertEqual(jobs.due(self.path, now=now + timedelta(days=1)), [])
        plan, _ = service.prepare_review(self.path, self.keys)
        lifecycle.commit(self.path, plan)
        with closing(orders.connection(self.path)) as db, db:
            sources.observe(db, self.order['online'], 'WAIT_ALLOCATION', '合成等待')
        self.assertEqual(jobs.due(self.path, now=now + timedelta(days=1)), [])

    def test_deleting_one_original_removes_its_pending_sources_and_checks(self):
        self.archive()
        with closing(orders.connection(self.path)) as db, db:
            orders.delete_orders(db, [(self.head['oid'], self.head['warehouse'])])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM jst_fulfillment_sources').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM jst_fulfillment_checks').fetchone()[0], 0)

    def test_read_only_browser_adapter_excludes_upload_and_recipient_data(self):
        calls = []
        class Lease:
            def request(self, action, **values):
                calls.append((action, values))
                return {}
        browser = service.Browser(Lease())
        browser.action('allocation', self.order, operation_id='a' * 32)
        self.assertEqual(calls[0][0], 'order.allocation')
        spec = calls[0][1]['spec']
        self.assertNotIn('file', spec)
        self.assertNotIn('lines', spec)
        self.assertEqual(spec['contact'], {'buyer': self.order['payload']['profile']['values']['buyer']})
        with self.assertRaises(ValueError):
            browser.action('import', self.order)

    def test_background_waits_for_this_warehouse_without_retiring_the_original(self):
        from catalog_sync_client import ComponentError
        from datetime import datetime, timedelta
        import jst_fulfillment_jobs as jobs
        class Lease:
            def request(self, action, **values):
                assert action == 'order.allocation' and values['retained_only'] is True
                raise ComponentError('SESSION_NOT_RETAINED', '合成本仓连接已关闭')
        result=service.pull(self.path,self.keys,service.Browser(Lease(),retained_only=True))
        self.assertEqual(result[0]['state'],'WAIT_CONNECTION')
        self.assertFalse(lifecycle.snapshot(self.path,self.head)['present']['payment'])
        self.assertEqual(jobs.due(self.path),[])
        self.assertEqual(jobs.due(self.path,now=datetime.fromisoformat(wr.now())+timedelta(minutes=6)),self.keys)

    def test_migration_disables_polling_but_keeps_sources_and_links(self):
        import jst_fulfillment_jobs as jobs
        import order_migration
        self.archive()
        jobs.configure(self.path, True)
        order_migration.pause_imported(self.path.parent)
        self.assertFalse(jobs.enabled(self.path))
        with closing(wr.read_connection(self.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM jst_fulfillment_sources').fetchone()[0], 1)
            self.assertEqual(len([key for key in payment_match_index.mappings(db) if key.startswith('payment_match.order.')]), 1)


if __name__ == '__main__':
    unittest.main()
