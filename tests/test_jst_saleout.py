from contextlib import closing
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import unittest
import openpyxl

import jst_fulfillment as service
import jst_fulfillment_store as sources
import order_lifecycle as lifecycle
import work_repository as wr
import test_jst_fulfillment as fixtures


class Saleout(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.Fulfillment();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.order=self.f.order;self.binding=self.order['proof']['binding']
        self.sku=self.order['payload']['lines'][0][0]

    def original(self, *, qty=12, parent=30, price='2.5', doc_status='已出库', duplicate=False, split=None):
        b=openpyxl.Workbook();s=b.active
        s.append(['出仓单号','店铺编号','店铺名称','内部订单号','线上订单号','状态','商品编码','名称','数量','应付金额','明细卖家实收'])
        docs = []
        for i,quantity in enumerate(split or [qty],9001):
            row=[str(i),self.binding['shop_id'],self.binding['shop'],self.order['internal'],self.order['online'],doc_status,self.sku,'测试商品',quantity,parent,0]
            s.append(row)
            if duplicate:s.append(row)
            docs.append({'io_id':str(i),'internal':self.order['internal'],'online':self.order['online'],
                         'coid':self.binding['coid'],'shop':self.binding['shop'],'shop_id':self.binding['shop_id'],
                         'status':doc_status,'quantity':str(quantity)})
        out=BytesIO();b.save(out);b.close();raw=out.getvalue()
        facts={'internal':self.order['internal'],'online':self.order['online'],'coid':self.binding['coid'],
               'shop':self.binding['shop'],'shop_id':self.binding['shop_id'],'native_status':'已发货','amount':str(parent),
               'items':[{'sku':self.sku,'qty':str(qty),'price':price,'amount':str(qty*float(price))}], 'observed':wr.now()}
        receipt={'source_kind':'jst_saleout_detail_v1','sha':sha256(raw).hexdigest(),'binding':self.binding,
                 'online':self.order['online'],'internal':self.order['internal'],'complete':True,'native_status':'已发货',
                 'order_facts':facts,'outbounds':docs}
        return raw,receipt

    def test_final_saleout_quantity_and_transaction_price_prepare_a_manual_review(self):
        raw,receipt=self.original();p=service.parse(raw,self.order,receipt)
        self.assertEqual((p['quantity'],p['amount']),('12','30.00'))
        self.assertEqual(p['rows'][0]['price'],'2.5')
        self.assertEqual(p['rows'][0]['values'][-1],0)
        with closing(fixtures.orders.connection(self.f.path)) as db,db:
            sources.save(db,self.order['online'],p,raw)
        plan,overview=service.prepare_review(self.f.path,self.f.keys)
        self.assertEqual(overview[0]['amount'],'30.00')
        self.assertFalse(lifecycle.snapshot(self.f.path,self.f.head)['present']['payment'])
        self.assertEqual(len(plan['plans']),1)

    def test_parent_money_is_not_multiplied_and_zero_settlement_is_not_used(self):
        raw,receipt=self.original(parent=999)
        with self.assertRaisesRegex(ValueError,'整单金额'):
            service.parse(raw,self.order,receipt)

    def test_same_sku_across_two_valid_outbounds_is_checked_then_grouped_for_payment(self):
        raw,receipt=self.original(split=[7,5])
        payload=service.parse(raw,self.order,receipt)
        self.assertEqual(len(payload['rows']),1)
        self.assertEqual((payload['quantity'],payload['amount']),('12','30.00'))
        self.assertEqual([source['outbound'] for source in payload['rows'][0]['sources']],['9001','9002'])
        with closing(fixtures.orders.connection(self.f.path)) as db,db:
            sources.save(db,self.order['online'],payload,raw)
        plan,_=service.prepare_review(self.f.path,self.f.keys)
        self.assertEqual(len(plan['plans'][0]['payload']['raw_rows']),1)

    def test_wrong_source_identity_partial_quantities_and_void_documents_stop(self):
        raw,receipt=self.original()
        for field,value in [('coid','wrong'),('shop_id','wrong'),('internal','wrong'),('native_status','待付款')]:
            changed=deepcopy(receipt);changed['order_facts'][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):
                service.parse(raw,self.order,changed)
        changed=deepcopy(receipt);changed['order_facts']['items'][0].update(qty='13',amount='32.5')
        with self.assertRaisesRegex(ValueError,'实配数量'):
            service.parse(raw,self.order,changed)
        changed=deepcopy(receipt);changed['outbounds'][0]['status']='出库前作废'
        with self.assertRaises(ValueError):service.parse(raw,self.order,changed)
        raw,receipt=self.original(duplicate=True)
        with self.assertRaisesRegex(ValueError,'重复明细'):
            service.parse(raw,self.order,receipt)


if __name__=='__main__':unittest.main()
