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
    change_lookup_quantity = False
    total = '35'

    def action(self, name, order, **kwargs):
        if name == 'payment_detail':
            self.calls.append((name, order['online']))
            existing = self.orders[order['online']]
            sku = order['payload']['lines'][0][0]
            return {'found': True, 'contact_ok': True, 'online': order['online'], 'internal': existing['internal'],
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
        self.assertEqual(lifecycle.snapshot(self.path,head)['status'],'待收货')
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

    def test_same_store_two_warehouse_candidates_only_feed_purchase_after_manual_payment(self):
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
        self.assertEqual(self.submit()['payment']['state'],'READY')
        self.portal.qty,self.portal.total = '9','26.75'
        response, = automatic.process(self.path,south_ready['ready'],self.portal,'submit',prepare_payment=service.pull_order_payment)
        self.assertEqual(response['payment']['state'],'READY')

        heads = [self.base.head,south]
        keys = self.keys + south_ready['ready']
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
