from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal
import unittest

import jst_audited_payment as audited
import jst_auto_order as automatic
import jst_auto_order_store as orders
import jst_batches
import jst_fulfillment as service
import jst_fulfillment_jobs as jobs
import jst_fulfillment_store as sources
import order_lifecycle as lifecycle
import test_jst
import test_jst_auto_order as fixture
import work_repository as wr
from aoxiang_purchase import freeze_payment
from warehouse_catalog import revision


class Platform(fixture.Portal):
    qty = '12'
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
                    'items': [{'sku':sku,'qty':self.qty,'price':'2.75','amount':str(Decimal(self.qty)*Decimal('2.75'))}],
                    'detail_count':1,'currency':'CNY','freight':'2','amount':self.total,'detail_error':self.detail_error}
        reply = super().action(name, order, **kwargs)
        if self.change_lookup_quantity and name == 'lookup' and reply.get('native_status') == audited.AUDITED:
            reply['lines'][0][1] = self.qty
        return reply


class AuditedPayment(unittest.TestCase):
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
        return automatic.process(self.path,self.keys,self.portal,'submit',prepare_payment=service.pull_audited)[0]

    def test_post_audit_candidate_is_automatic_and_retains_quantity_difference(self):
        self.portal.change_lookup_quantity = True
        response = self.submit()
        self.assertEqual((response['stage'],response['payment']['state']),('AUDITED','READY'))
        self.assertEqual([event for event,_ in self.portal.calls].count('payment_detail'),1)
        self.assertFalse(lifecycle.snapshot(self.path,self.base.head)['present']['payment'])
        plan, summary = service.prepare_review(self.path,self.keys)
        self.assertEqual(summary[0]['source'],audited.LABEL)
        self.assertEqual(summary[0]['totals'],{'goods':'33.00','freight':'2.00','payable':'35.00','unexplained':'0.00'})
        self.assertNotEqual(summary[0]['ordered'],summary[0]['quantity'])
        # The test deliberately performs the action reserved for business confirmation.
        lifecycle.commit(self.path,plan)
        self.assertEqual(len(lifecycle.history(self.path,self.base.head,'payment')),1)
        self.assertEqual(plan['plans'][0]['payload']['raw_rows'][0]['base_quantity'],'12')

    def test_disconnect_keeps_completed_audit_and_restarts_by_same_internal_order(self):
        self.portal.detail_error = '合成断线，原单整单明细暂不可读'
        response = self.submit()
        self.assertEqual((response['stage'],response['payment']['state']),('AUDITED','RETRY_PAYMENT_READ'))
        self.assertEqual(jobs.due_audited(self.path,now=datetime.fromisoformat(wr.now())+timedelta(minutes=6)),self.keys)
        self.portal.detail_error = ''
        response = service.pull_audited(self.path,self.keys,self.portal)[0]
        self.assertEqual(response['state'],'READY')
        self.assertEqual([event for event,_ in self.portal.calls].count('import'),1)
        self.assertEqual(jobs.waiting_review(self.path),self.keys)

    def test_duplicate_facts_reuse_candidate_but_changed_facts_block_old_plan(self):
        first = self.submit()['payment']
        again = service.pull_audited(self.path,self.keys,self.portal)[0]
        self.assertEqual(first['digest'],again['digest'])
        plan,_ = service.prepare_review(self.path,self.keys)
        self.portal.qty,self.portal.total='8','24'
        revised = service.pull_audited(self.path,self.keys,self.portal)[0]
        self.assertNotEqual(revised['digest'],first['digest'])
        with self.assertRaisesRegex(ValueError,'资料已更新'):
            lifecycle.commit(self.path,plan)
        self.assertFalse(lifecycle.snapshot(self.path,self.base.head)['present']['payment'])
        with closing(wr.read_connection(self.path)) as db:
            self.assertEqual(len(sources.history(db,self.online)),2)

    def test_canceled_or_withdrawn_status_invalidates_candidate_and_keeps_original(self):
        for status in ('已付款待审核','已取消'):
            with self.subTest(status=status):
                if status=='已付款待审核':
                    self.submit()
                else:
                    self.portal.orders[self.online]['native_status']=audited.AUDITED
                    service.pull_audited(self.path,self.keys,self.portal)
                plan,_ = service.prepare_review(self.path,self.keys)
                self.portal.orders[self.online]['native_status']=status
                self.assertEqual(service.pull_audited(self.path,self.keys,self.portal)[0]['state'],'PAYMENT_SOURCE_PENDING')
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
            audited.prepare(order,receipt)
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
        response, = automatic.process(self.path,south_ready['ready'],self.portal,'submit',prepare_payment=service.pull_audited)
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
