from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import unittest
from unittest.mock import patch

import daily_batch
import jst_order_confirmation as confirmation
import jst_order_payment as payment
import jst_auto_order as automatic
import jst_auto_order_store as orders
import jst_batches
import jst_fulfillment as service
import jst_fulfillment_jobs as jobs
import jst_fulfillment_store as sources
import order_analytics
import order_lifecycle as lifecycle
import test_jst
import test_jst_auto_order as fixture
import work_repository as wr
from aoxiang_purchase import freeze_payment
from warehouse_catalog import revision


class Platform(fixture.Portal):
    qty = '12'
    price = '2.75'
    freight = '2'
    detail_error = ''
    detail_buyer_check = 'verified'
    change_lookup_quantity = False
    total = '35'

    def action(self, name, order, **kwargs):
        if name == 'payment_detail':
            self.calls.append((name, order['online']))
            existing = self.orders[order['online']]
            sku = order['payload']['lines'][0][0]
            return {'found': True, 'contact_ok': self.detail_buyer_check == 'verified', 'buyer_check': self.detail_buyer_check,
                    'online': order['online'], 'internal': existing['internal'],
                    'native_status': existing.get('native_status'), 'binding': order['proof']['binding'],
                    'items': [{'sku':sku,'qty':self.qty,'price':self.price,'amount':str(Decimal(self.qty)*Decimal(self.price))}],
                    'detail_count':1,'currency':'CNY','freight':self.freight,'amount':self.total,'detail_error':self.detail_error}
        reply = super().action(name, order, **kwargs)
        if self.change_lookup_quantity and name == 'lookup' and reply.get('native_status') == confirmation.AWAITING:
            reply['lines'][0][1] = self.qty
        return reply


class OrderPayment(unittest.TestCase):
    def setUp(self):
        self.base = fixture.AutoOrders()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.path = self.base.path
        self.keys = self.base.prepared()
        self.online = self.keys[0]
        self.portal = Platform()
        automatic.process(self.path, self.keys, self.portal, 'preview')

    def submit(self):
        return automatic.process(self.path,self.keys,self.portal,'submit',prepare_payment=service.pull_order_payment)[0]

    def test_original_lookup_waits_for_buyer_verification_then_writes_and_pulls_once(self):
        original = self.base.read(self.online)
        automatic._save(self.path, original, 'IMPORT_UNKNOWN', None, reason='合成原任务已发起导入')
        remote = {'internal':'1333805','status':'audited','native_status':confirmation.AUDITED}
        self.portal.orders[self.online] = remote
        for buyer_check, message in (('unavailable','原单身份待核'),('mismatch','完整买家账号与原任务不同')):
            with self.subTest(buyer_check=buyer_check):
                remote.update(buyer_check=buyer_check,contact_ok=False)
                result, = automatic.process(self.path,self.keys,self.portal,'recover',prepare_payment=service.pull_order_payment)
                self.assertEqual(result['stage'],'IMPORT_UNKNOWN')
                self.assertIn(message,result['reason'])
                self.assertIsNone(self.base.read(self.online)['internal'])
                self.assertNotIn('payment',result)
                self.assertFalse(jobs.waiting_review(self.path))
        remote.update(buyer_check='verified',contact_ok=True)
        first, = automatic.process(self.path,self.keys,self.portal,'recover',prepare_payment=service.pull_order_payment)
        second, = automatic.process(self.path,self.keys,self.portal,'recover',prepare_payment=service.pull_order_payment)
        self.assertEqual((first['stage'],first['payment']['state']),('AUDITED','READY'))
        self.assertEqual(first['payment']['digest'],second['payment']['digest'])
        self.assertEqual(self.base.read(self.online)['internal'],'1333805')
        self.assertEqual(jobs.waiting_review(self.path),[self.online])
        with closing(wr.read_connection(self.path)) as db:
            self.assertEqual(len(sources.history(db,self.online)),1)
        self.assertFalse(lifecycle.snapshot(self.path,self.base.head)['present']['payment'])
        self.assertEqual([a for a,_ in self.portal.calls],['preview','lookup','lookup','lookup','payment_detail','lookup','payment_detail'])

    def test_masked_buyer_recovers_each_warehouse_by_its_frozen_preview_and_full_detail(self):
        _, south_keys = self.prepare_south()
        keys = self.keys + south_keys
        receipts = {}
        for online, internal, status in zip(keys, ('90011', '90012'), (confirmation.AWAITING, confirmation.AUDITED)):
            order = self.base.read(online)
            automatic._save(self.path, order, 'IMPORT_UNKNOWN', None, reason='合成原任务已发起导入')
            items = [{'sku': sku, 'qty': qty, 'price': price, 'amount': str(Decimal(qty)*Decimal(price))}
                     for sku, qty, price in order['payload']['lines']]
            receipts[online] = {'found': True, 'buyer_check': 'unavailable', 'contact_ok': False,
                                'online': online, 'internal': internal, 'native_status': status,
                                'binding': order['proof']['binding'], 'items': items, 'detail_count': len(items),
                                'currency': 'CNY', 'freight': '0', 'amount': order['proof']['amount']}

        calls = []

        def read(name, order, **kwargs):
            self.assertIn(name, ('lookup', 'payment_detail'))
            calls.append((name, order['online']))
            return receipts[order['online']]

        def recover(online):
            result, = automatic.process(self.path, [online], self.portal, 'recover', prepare_payment=service.pull_order_payment)
            return result

        first, second = keys
        valid = receipts[first]
        item, = valid['items']
        # Same order totals can hide wrong units, price or product.
        for changes, hint in (
            ({'binding': receipts[second]['binding']}, '企业、店铺或线上单号'),
            ({'items': []}, '商品款数'),
            ({'items': [{**item, 'sku': '0002'}]}, '原仓单外商品'),
            ({'items': [{**item, 'qty': '20', 'price': '4'}]}, '数量与原生成单不同'),
            ({'buyer_check': 'mismatch'}, '完整买家账号与原任务不同'),
            ({'buyer_check': 'masked_conflict'}, '隐号'),
            ({'native_status': '已取消', 'items': []}, '原单身份待核'),
        ):
            with self.subTest(changes=changes), patch.object(self.portal, 'action', side_effect=read):
                receipts[first] = {**valid, **changes}
                self.assertIn(hint, recover(first)['reason'])
                self.assertIsNone(self.base.read(first)['internal'])
                self.assertEqual(jobs.waiting_review(self.path), [])

        receipts[first] = valid
        calls.clear()
        with patch.object(self.portal, 'action', side_effect=read):
            for online in keys:
                result = recover(online)
                self.assertEqual(result['internal'], receipts[online]['internal'], result['reason'])
                self.assertEqual(result['payment']['state'], 'READY')
                self.assertEqual(recover(online)['payment']['digest'], result['payment']['digest'])
            self.assertEqual(calls, [(action, online) for online in keys
                                     for action in ('lookup', 'payment_detail')*2])
            plan, _ = service.prepare_review(self.path, keys)
            with patch.object(service, 'run_order_payment', side_effect=lambda *_: service.pull_order_payment(self.path, keys, self.portal)):
                service.verify_order_before_commit(self.path, plan['jst_fulfillment'], None)
                receipts[first] = {**valid, 'buyer_check': 'mismatch'}
                with self.assertRaisesRegex(wr.Conflict, '完整买家账号与原任务不同'):
                    service.verify_order_before_commit(self.path, plan['jst_fulfillment'], None)
        with closing(wr.read_connection(self.path)) as db:
            for online in keys:
                order = self.base.read(online)
                self.assertEqual(order['internal'], valid['internal'] if online == first else receipts[online]['internal'])
                self.assertEqual(len(sources.history(db, online)), 1)
                self.assertIsNone(lifecycle.current(db, order['payload']['head'], 'payment'))

    def test_masked_buyer_can_bind_original_with_line_amount_pending(self):
        order = self.base.read(self.online)
        automatic._save(self.path, order, 'IMPORT_UNKNOWN', None, reason='合成平台已落原单')
        sku, qty, price = order['payload']['lines'][0]
        item = {'sku': sku, 'qty': qty, 'price': price, 'amount': '79.99'}
        receipt = {'found': True, 'buyer_check': 'unavailable', 'contact_ok': False,
                   'online': self.online, 'internal': '90013', 'native_status': confirmation.AWAITING,
                   'binding': order['proof']['binding'], 'items': [item], 'detail_count': 1,
                   'currency': 'CNY', 'freight': '0', 'amount': '79.99'}
        with patch.object(self.portal, 'action', return_value=receipt) as read:
            result, = automatic.process(self.path, self.keys, self.portal, 'recover',
                                        prepare_payment=service.pull_order_payment)
            self.assertEqual((result['stage'], result['internal']), ('IMPORTED', '90013'))
            self.assertEqual(result['payment']['state'], 'NEEDS_REVIEW')
            self.assertIn(f'商品 {sku}', result['payment']['message'])
            self.assertIn('平台行金额 79.99 元', result['payment']['message'])
            self.assertIn('差额 -0.01 元', result['payment']['message'])
            self.assertEqual(jobs.waiting_review(self.path), [])
            self.assertFalse(lifecycle.snapshot(self.path,self.base.head)['present']['payment'])
            item['amount'] = receipt['amount'] = '80.00'
            result, = automatic.process(self.path, self.keys, self.portal, 'recover',
                                        prepare_payment=service.pull_order_payment)
        self.assertEqual(result['internal'], '90013')
        self.assertEqual(result['payment']['state'], 'READY')
        self.assertEqual([call.args[0] for call in read.call_args_list], ['lookup','payment_detail']*2)
        self.assertEqual([op for op, _ in self.portal.calls], ['preview'])

    def test_bound_original_can_read_cancelled_status_with_hidden_buyer(self):
        self.submit()
        order = self.base.read(self.online)
        original = self.portal.orders[self.online]
        original.update(native_status='已取消', status='review', buyer_check='unavailable', contact_ok=False)
        for changes in ({'buyer_check':'mismatch'}, {'buyer_check':'masked_conflict'},
                        {'internal':'6002'}, {'detail_error':'原生整单明细身份待核'}):
            with self.subTest(changes=changes), patch.dict(original, changes):
                check, = automatic.process(self.path, self.keys, self.portal, 'recover')
                self.assertFalse(check['ok'])
                self.assertEqual(check['stage'], 'IMPORTED')
        check, = automatic.process(self.path, self.keys, self.portal, 'recover')
        self.assertEqual((check['stage'],check['internal']), ('CANCELLED',order['internal']))
        with closing(wr.read_connection(self.path)) as db:
            from jst_order_store import active, locked
            self.assertEqual(active(db,order['payload']['head']), order['gid'])
            self.assertEqual(locked(db,order['payload']['head']), order['gid'])
        self.assertEqual([op for op, _ in self.portal.calls].count('import'), 1)

    def test_unbound_cancelled_original_can_redo_with_verified_native_items(self):
        from jst_cancellation import begin_redo
        from jst_order_store import active, locked

        _, south_keys = self.prepare_south()
        keys = self.keys + south_keys
        originals = {online: self.base.read(online) for online in keys}
        for online, internal in zip(keys, ('90017', '90018')):
            order = originals[online]
            automatic._save(self.path, order, 'IMPORT_UNKNOWN', None, reason='合成平台已导入，内部号尚未回写')
            sku, quantity, price = order['payload']['lines'][0]
            # The cancelled line retains the inventory quantity; its former
            # price and refund state are not evidence of a new payment.
            items = [{'sku':sku, 'qty':quantity, 'price':price, 'amount':'0',
                      'item_status':'Cancelled', 'refund_status':'Refunded'}]
            self.portal.orders[online] = {'internal':internal, 'status':'review', 'native_status':'已取消',
                                          'buyer_check':'unavailable', 'contact_ok':False,
                                          'detail_count':1, 'items':items}

        for online, peer in (keys, keys[::-1]):
            order = originals[online]
            head = order['payload']['head']
            remote = self.portal.orders[online]
            item, = remote['items']
            cases = (
                ({'native_status':'被合并'}, '被合并'),
                ({'buyer_check':'masked_conflict'}, '隐号'),
                ({'buyer_check':'mismatch'}, '完整买家账号与原任务不同'),
                ({'binding': originals[peer]['proof']['binding']}, '企业、店铺或线上单号'),
                ({'items': [], 'detail_count':0}, '商品款数'),
                ({'detail_error':'原生整单明细身份不同'}, '整单明细身份不同'),
                ({'items':[dict(item, qty='1')]}, '数量与原生成单不同'),
                ({'items':[dict(item, sku='other')]}, '原仓单外商品'),
            )
            for changes, hint in cases:
                with self.subTest(warehouse=head['warehouse'], changes=changes), patch.dict(remote, changes):
                    check, = automatic.process(self.path, [online], self.portal, 'recover')
                    self.assertFalse(check['ok'])
                    self.assertIn(hint, check['reason'])
                    self.assertIsNone(self.base.read(online)['internal'])
                    with self.assertRaisesRegex(ValueError, '明确返回'):
                        begin_redo(self.path, online)
            check, = automatic.process(self.path, [online], self.portal, 'recover')
            self.assertEqual((check['stage'], check['internal']), ('CANCELLED', remote['internal']))
            with closing(wr.read_connection(self.path)) as db:
                self.assertEqual(active(db, head), order['gid'])
                self.assertEqual(locked(db, head), order['gid'])
                self.assertIsNone(lifecycle.current(db, head, 'payment'))
            redo = begin_redo(self.path, online)
            self.assertNotEqual(redo['online'], online)
            self.assertEqual(redo['released'], {item['sku']:item['qty']})
            self.assertEqual(begin_redo(self.path, online), redo)
            with closing(wr.read_connection(self.path)) as db:
                self.assertIsNone(active(db, head))
                self.assertIsNone(locked(db, head))
        self.assertFalse([action for action, _ in self.portal.calls if action in ('import','audit')])

    def test_insufficient_buyer_evidence_or_conflict_blocks_payment_recheck(self):
        ready = self.submit()['payment']
        plan, _ = service.prepare_review(self.path,self.keys)
        self.portal.orders[self.online]['native_status'] = confirmation.AUDITED
        for buyer_check, message in (('unavailable','原单身份待核'),('mismatch','完整买家账号与原任务不同')):
            with self.subTest(buyer_check=buyer_check):
                self.portal.detail_buyer_check = buyer_check
                result, = service.pull_order_payment(self.path,self.keys,self.portal)
                self.assertEqual(result['state'],'NEEDS_REVIEW')
                self.assertIn(message,result['message'])
                self.assertEqual(self.base.read(self.online)['stage'],'IMPORTED')
                self.assertFalse(jobs.waiting_review(self.path))
                with patch.object(service,'run_order_payment',side_effect=lambda *args: service.pull_order_payment(self.path,self.keys,self.portal)):
                    with self.assertRaisesRegex(wr.Conflict,message):
                        service.verify_order_before_commit(self.path,plan['jst_fulfillment'],None)
                self.assertFalse(lifecycle.snapshot(self.path,self.base.head)['present']['payment'])
                with closing(wr.read_connection(self.path)) as db:
                    self.assertEqual(sources.current(db,self.online)['digest'],ready['digest'])

    def test_imported_candidate_is_automatic_and_retains_quantity_difference(self):
        self.portal.change_lookup_quantity = True
        response = self.submit()
        self.assertEqual((response['stage'],response['payment']['state']),('IMPORTED','READY'))
        self.assertEqual([action for action,_ in self.portal.calls],['preview','stage_import','import','lookup','payment_detail'])
        self.assertEqual([event for event,_ in self.portal.calls].count('payment_detail'),1)
        self.assertFalse(lifecycle.snapshot(self.path,self.base.head)['present']['payment'])
        plan, summary = service.prepare_review(self.path,self.keys)
        self.assertEqual(summary[0]['source'],payment.LABEL)
        self.assertEqual(summary[0]['totals'],{'goods':'33.00','freight':'2.00','payable':'35.00','unexplained':'0.00'})
        self.assertNotEqual(summary[0]['ordered'],summary[0]['quantity'])
        # The test deliberately performs the action reserved for business confirmation.
        lifecycle.commit(self.path,plan)
        self.assertEqual(len(lifecycle.history(self.path,self.base.head,'payment')),1)
        self.assertEqual(plan['plans'][0]['payload']['raw_rows'][0]['base_quantity'],'12')

    def test_import_counts_actual_order_but_stays_in_default_payment_inbox(self):
        head = self.base.head
        self.assertEqual(lifecycle.snapshot(self.path,head)['status'],'待付款')
        self.submit()
        view = lifecycle.snapshot(self.path,head)
        self.assertEqual((view['status'],view['next']),('待付款','payment'))
        self.assertFalse(view['present']['payment'])
        self.assertFalse(view['health']['payment'])
        self.assertTrue(view['health'][lifecycle.JST_ORDER])
        waiting = daily_batch.read(self.path,self.base.f.f.root/'progress.sqlite',
            {'stage_view':'payment','stage_filter':'待处理','all_matched':True})
        self.assertEqual([r['number'] for r in waiting['records']],[head['number']])

        report = order_analytics.collect(self.path,{'from':head['day'],'to':head['day'],'lifecycle_from':'payment'})
        row, = [r for r in report['rows'] if r['order']==head['number'] and r['sku']=='0001']
        self.assertEqual(row['quantities']['payment'],12)
        self.assertTrue(row['valid']['payment'])
        self.assertIsNone(row['payment_amount'])
        self.assertEqual(report['payments'],[])

        plan, _ = service.prepare_review(self.path,self.keys)
        lifecycle.commit(self.path,plan)
        receipt_request = {'stage_view':'receipt','stage_filter':'待处理','all_matched':True}
        cache = self.base.f.f.root/'progress.sqlite'
        self.assertEqual(lifecycle.snapshot(self.path,head)['status'],'待发货')
        self.assertEqual(daily_batch.read(self.path,cache,receipt_request)['records'],[])
        for state in (confirmation.AUDITED, confirmation.SHIPPED):
            self.portal.orders[self.online]['native_status'] = state
            result, = service.pull_order_payment(self.path,self.keys,self.portal)
            self.assertEqual(result['state'],'MATCHES_PAYMENT')
            self.assertEqual(lifecycle.snapshot(self.path,head)['status'],'待发货')
            self.assertEqual(daily_batch.read(self.path,cache,receipt_request)['records'],[])

        self.portal.orders[self.online]['native_status'] = confirmation.DELIVERING
        self.portal.detail_buyer_check = 'mismatch'
        service.pull_order_payment(self.path,self.keys,self.portal)
        self.assertEqual(lifecycle.snapshot(self.path,head)['status'],'待发货')
        self.portal.detail_buyer_check = 'verified'
        service.pull_order_payment(self.path,self.keys,self.portal)
        self.assertEqual(lifecycle.snapshot(self.path,head)['status'],'待收货')
        self.portal.orders[self.online]['native_status'] = confirmation.SHIPPED
        service.pull_order_payment(self.path,self.keys,self.portal)
        self.assertEqual([r['number'] for r in daily_batch.read(self.path,cache,receipt_request)['records']], [head['number']])
        self.assertEqual(len(lifecycle.history(self.path,head,'payment')),1)
        form = self.base.f.f
        receipt, _ = form.save('receipt',[{'实际收货数量':11}])
        saved = receipt['plans'][0]['payload']
        self.assertEqual(saved['basis']['stage'],'payment')
        self.assertEqual(saved['basis']['products']['0001']['quantity'],'12')
        self.assertFalse(saved['pending'])
        current = lifecycle.snapshot(self.path,head)
        self.assertTrue(current['health']['payment'])
        self.assertTrue(current['health']['receipt'])
        self.assertEqual(current['status'],'已收齐')
        self.assertEqual(len(lifecycle.history(self.path,head,lifecycle.JST_ORDER)),1)

    def test_normal_status_progression_can_save_same_original_candidate(self):
        first = self.submit()['payment']
        plan, _ = service.prepare_review(self.path,self.keys)
        for status in (confirmation.AUDITED,*confirmation.SHIPPING):
            self.portal.orders[self.online]['native_status'] = status
            with patch.object(service,'run_order_payment',side_effect=lambda *args: service.pull_order_payment(self.path,self.keys,self.portal)):
                service.verify_order_before_commit(self.path,plan['jst_fulfillment'],None)
            with closing(wr.read_connection(self.path)) as db:
                self.assertEqual(sources.current(db,self.online)['digest'],first['digest'])
        lifecycle.commit(self.path,plan)
        self.assertEqual(len(lifecycle.history(self.path,self.base.head,'payment')),1)
        self.assertEqual(len(lifecycle.history(self.path,self.base.head,lifecycle.JST_ORDER)),1)
        self.assertEqual([event for event,_ in self.portal.calls if event in ('import','audit')],['import'])

    def test_upgrade_reuses_legacy_candidate_when_live_facts_match(self):
        automatic.process(self.path,self.keys,self.portal,'submit')
        order = self.base.read(self.online)
        candidate,_ = payment.prepare(order,self.portal.action('payment_detail',order))
        raw = b'historical audited candidate fixture'
        candidate.update(source_kind=payment.LEGACY_KIND,native_status=confirmation.AUDITED,
                         file={'name':'legacy.json','sha':sha256(raw).hexdigest()})
        with closing(orders.connection(self.path)) as db,db:
            old = sources.save(db,self.online,candidate,raw)
        plan,_ = service.prepare_review(self.path,self.keys)
        self.portal.orders[self.online]['native_status'] = '已发货'
        result, = service.pull_order_payment(self.path,self.keys,self.portal)
        self.assertEqual((result['state'],result['digest']),('READY',old))
        self.assertEqual(plan['jst_fulfillment'][0]['digest'],old)
        lifecycle.commit(self.path,plan)
        with closing(wr.read_connection(self.path)) as db:
            self.assertEqual(len(sources.history(db,self.online)),1)

    def test_disconnect_keeps_imported_order_and_restarts_by_same_internal_number(self):
        self.portal.detail_error = '合成断线，原单整单明细暂不可读'
        response = self.submit()
        self.assertEqual((response['stage'],response['payment']['state']),('IMPORTED','RETRY_PAYMENT_READ'))
        self.assertEqual(self.base.read(self.online)['internal'],'6001')
        self.assertEqual(jobs.due_order_payment(self.path,now=datetime.fromisoformat(wr.now())+timedelta(minutes=6)),self.keys)
        self.portal.detail_error = ''
        response = service.pull_order_payment(self.path,self.keys,self.portal)[0]
        self.assertEqual(response['state'],'READY')
        self.assertEqual([event for event,_ in self.portal.calls].count('import'),1)
        self.assertEqual(jobs.waiting_review(self.path),self.keys)

    def test_duplicate_facts_reuse_candidate_but_changed_facts_block_old_plan(self):
        first = self.submit()['payment']
        again = service.pull_order_payment(self.path,self.keys,self.portal)[0]
        self.assertEqual(first['digest'],again['digest'])
        plan,_ = service.prepare_review(self.path,self.keys)
        self.portal.qty,self.portal.total='8','24'
        revised = service.pull_order_payment(self.path,self.keys,self.portal)[0]
        self.assertNotEqual(revised['digest'],first['digest'])
        with self.assertRaisesRegex(ValueError,'资料已更新'):
            lifecycle.commit(self.path,plan)
        self.assertFalse(lifecycle.snapshot(self.path,self.base.head)['present']['payment'])
        with closing(wr.read_connection(self.path)) as db:
            self.assertEqual(len(sources.history(db,self.online)),2)

    def test_canceled_or_withdrawn_status_invalidates_candidate_and_keeps_original(self):
        for status in ('待付款','已取消'):
            with self.subTest(status=status):
                if status=='待付款':
                    self.submit()
                else:
                    self.portal.orders[self.online]['native_status']=confirmation.AWAITING
                    service.pull_order_payment(self.path,self.keys,self.portal)
                plan,_ = service.prepare_review(self.path,self.keys)
                self.portal.orders[self.online]['native_status']=status
                self.assertEqual(service.pull_order_payment(self.path,self.keys,self.portal)[0]['state'],'PAYMENT_SOURCE_PENDING')
                with self.assertRaises(ValueError):
                    lifecycle.commit(self.path,plan)
                with closing(wr.read_connection(self.path)) as db:
                    self.assertTrue(sources.history(db,self.online))
        self.assertEqual(jobs.waiting_review(self.path),[])

    def test_stale_generation_keeps_history_and_blocks_manual_save(self):
        self.submit()
        plan,_ = service.prepare_review(self.path,self.keys)
        with closing(orders.connection(self.path)) as db,db:
            db.execute("UPDATE jst_active SET gid='synthetic-superseded'")
        with self.assertRaisesRegex(ValueError,'生成版本已变化'):
            lifecycle.commit(self.path,plan)
        self.assertEqual(jobs.waiting_review(self.path),[])

    def test_existing_generated_payment_is_preserved_and_equal_facts_do_not_add_version(self):
        import jst_orders
        automatic.process(self.path,self.keys,self.portal,'submit')
        lifecycle.commit(self.path,jst_orders.payment_plan(self.path,self.base.gid))
        original, = lifecycle.history(self.path,self.base.head,'payment')

        different = service.pull_order_payment(self.path,self.keys,self.portal)[0]
        self.assertEqual(different['state'],'READY')
        plan, overview = service.prepare_review(self.path,self.keys)
        self.assertEqual(overview[0]['previous_payment']['version'],1)
        self.assertEqual(overview[0]['previous_payment']['changed_skus'],1)
        self.assertEqual(plan['plans'][0]['expected'],1)
        self.assertEqual(lifecycle.history(self.path,self.base.head,'payment'),[original])

        self.portal.qty,self.portal.price,self.portal.total,self.portal.freight='40','2','80','0'
        identical = service.pull_order_payment(self.path,self.keys,self.portal)[0]
        self.assertEqual(identical['state'],'MATCHES_PAYMENT')
        with self.assertRaisesRegex(ValueError,'沿用该版本'):
            service.prepare_review(self.path,self.keys)
        self.assertEqual(lifecycle.history(self.path,self.base.head,'payment'),[original])

    def test_recovery_must_recheck_original_before_resuming_native_payment(self):
        import lifecycle_recovery
        self.submit()
        plan,_ = service.prepare_review(self.path,self.keys)
        task = lifecycle_recovery.begin(self.path,plan)
        with self.assertRaisesRegex(ValueError,'原订单须在提交前重新回查'):
            lifecycle_recovery.recover(self.path,task['id'])
        self.assertEqual(lifecycle.history(self.path,self.base.head,'payment'),[])
        with patch.object(service,'run_order_payment',side_effect=lambda *args: service.pull_order_payment(self.path,self.keys,self.portal)) as read:
            self.assertEqual(lifecycle_recovery.recover(self.path,task['id'],order_session=object())['action'],'completed')
            read.assert_called_once()
        self.assertEqual(len(lifecycle.history(self.path,self.base.head,'payment')),1)

    def test_unreconciled_discount_or_unknown_units_do_not_create_payable_candidate(self):
        self.portal.total='31'
        result=self.submit()['payment']
        self.assertEqual(result['state'],'NEEDS_REVIEW')
        self.assertIn('优惠或其他金额差异 -4.00',result['message'])
        order=self.base.read(self.online)
        self.portal.total='35'
        receipt=self.portal.action('payment_detail',order)
        receipt['items'][0].update(sales_unit='箱',per_unit='12')
        with self.assertRaisesRegex(ValueError,'单位换算'):
            payment.prepare(order,receipt)
        self.assertEqual(jobs.waiting_review(self.path),[])

    def prepare_south(self):
        south = next(h for h in self.base.f.f.heads if h['warehouse']=='山东仓')
        self.base.f.f.save('return',[{'补货单号':south['number'],'供货仓':'山东仓','商品编码':'0002','门店确认数量':18}])
        catalog = test_jst.catalog('山东仓')
        catalog['items']['0002'] = {**catalog['items'].pop('0001'),'商品编码':'0002'}
        catalog['revision'] = revision(catalog['items'])
        generated = jst_batches.execute(self.path,jst_batches.preview_new(self.path,[south],{'山东仓':catalog}))
        self.base.configure(south,{**fixture.CONTACT,'buyer':'000888','address':'合成山东收货点'})
        south_ready = automatic.prepare(self.path,[{'head':south,'gid':generated['generated'][0]}])
        self.assertEqual(south_ready['failed'],[])
        automatic.process(self.path,south_ready['ready'],self.portal,'preview')
        return south, south_ready['ready']

    def test_same_store_two_warehouse_candidates_only_feed_purchase_after_manual_payment(self):
        south, south_keys = self.prepare_south()
        self.assertEqual(self.submit()['payment']['state'],'READY')
        self.portal.qty,self.portal.total = '9','26.75'
        response, = automatic.process(self.path,south_keys,self.portal,'submit',prepare_payment=service.pull_order_payment)
        self.assertEqual(response['payment']['state'],'READY')

        heads = [self.base.head,south]
        keys = self.keys + south_keys
        config = {'configured':True,'enabled':True,'account_binding':{'account_id':'QA'},
                  'store_mapping_version':'qa-v1','targets':[{'buyerWarehouseCode':south['store']}]}
        suppliers = {'河北仓':'QA-HB','山东仓':'QA-SD'}
        for head in heads:
            with self.assertRaisesRegex(ValueError,'尚未确认'):
                freeze_payment(self.path,head,config,suppliers)
        plan,overview = service.prepare_review(self.path,keys)
        self.assertEqual([(r['head']['warehouse'],r['internal'],r['quantity'],r['amount']) for r in overview],
                         [('河北仓','6001','12','33.00'),('山东仓','6002','9','24.75')])
        lifecycle.commit(self.path,plan)
        both = [freeze_payment(self.path,h,config,suppliers) for h in heads]
        self.assertEqual([(p['supplier_code'],p['rows'][0]['sku'],p['rows'][0]['document_number'],
                           p['total_quantity'],p['payment_total_amount']) for p in both],
                         [('QA-HB','0001','6001','12','33'),('QA-SD','0002','6002','9','24.75')])
        self.assertEqual([lifecycle.snapshot(self.path,h)['present']['receipt'] for h in heads],[False,False])
        self.assertEqual([lifecycle.snapshot(self.path,h)['status'] for h in heads],['待发货','待发货'])
        for online, statuses in zip(keys, (['待收货','待发货'], ['待收货','待收货'])):
            self.portal.orders[online]['native_status'] = confirmation.DELIVERING
            [verified] = automatic.process(self.path,[online],self.portal,'recover')
            self.assertTrue(verified['ok'], verified)
            self.assertEqual([lifecycle.snapshot(self.path,h)['status'] for h in heads],statuses)
