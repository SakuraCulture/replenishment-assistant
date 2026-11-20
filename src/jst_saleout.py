"""Verified native sale-out detail plus the same order's complete transaction lines.

The sale-out workbook repeats parent amounts on every SKU and exposes zero-valued
platform settlement columns. Neither is a transaction-line amount. Quantity is
accepted only from final sale-out documents; money comes from the matched native
order line and must reconcile per SKU and against the distinct parent total.
"""
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256

from jst_allocation import number
from payment_evidence import HEAD_KEYS
from runtime_support import checkpoint

KIND='jst_saleout_detail_v1'


def identifier(value):
    if isinstance(value,float) and value.is_integer():
        value=int(value)
    return str(value).strip()


def parse(raw,order,receipt,cancel=None):
    from jst_fulfillment import _sheets
    p=order['payload'];binding=(order['proof'] or {}).get('binding')
    if (receipt.get('source_kind')!=KIND or not raw or len(raw)>32*1024*1024
            or sha256(raw).hexdigest()!=receipt.get('sha') or receipt.get('complete') is not True
            or receipt.get('binding')!=binding or receipt.get('online')!=order['online']
            or receipt.get('internal')!=order['internal'] or receipt.get('native_status')!='已发货'):
        raise ValueError('原生出库原件、身份或完整性回执不符')
    facts=receipt.get('order_facts') or {}
    if any(facts.get(k)!=v for k,v in {'internal':order['internal'],'online':order['online'],
             'coid':binding['coid'],'shop':binding['shop'],'shop_id':binding['shop_id'],'native_status':'已发货'}.items()):
        raise ValueError('出库取价明细不是同一企业、店铺和已发货原单')
    expected={sku:Decimal(qty) for sku,qty,_ in p['lines']}
    items={}
    for item in facts.get('items',[]):
        sku=item.get('sku')
        if sku not in expected or sku in items:
            raise ValueError('原生成交明细商品缺失、重复或超出原单')
        if item.get('item_status') in ('Cancelled','取消') or item.get('refund_status') in ('Success','退款成功'):
            raise ValueError('原单有取消或退款商品，不能自动当作有效实配')
        qty=number(item.get('qty'),'原生商品数量')
        price=number(item.get('price'),'原生商品成交价');amount=number(item.get('amount'),'原生商品金额')
        if min(qty,price,amount)<0 or (qty*price).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)!=amount.quantize(Decimal('.01'),rounding=ROUND_HALF_UP):
            raise ValueError('原生商品成交数量、单价和金额不一致')
        items[sku]=(qty,price,amount)
    if set(items)!=set(expected):
        raise ValueError('原生成交明细未覆盖原单全部商品，不能把缺行当0')
    documents={}
    for doc in receipt.get('outbounds',[]):
        io=doc.get('io_id')
        if not io or io in documents or doc.get('status')!='已出库':
            raise ValueError('出库单重复、状态不明或尚未完成出库')
        if any(doc.get(k)!=v for k,v in {'internal':order['internal'],'online':order['online'],
                'coid':binding['coid'],'shop':binding['shop'],'shop_id':binding['shop_id']}.items()):
            raise ValueError('出库单的企业、店铺或原单关联不符')
        documents[io]=doc
    if not documents:
        raise ValueError('没有完整的有效出库单，未生成付款资料')
    rows={};seen=set();doc_seen=set();by_doc={}
    required=('出仓单号','店铺编号','店铺名称','内部订单号','线上订单号','状态','商品编码','数量')
    for title,values in _sheets(raw):
        iterator=iter(values);headers=next(iterator,())
        if not all(key in headers for key in required):
            raise ValueError('原生出库明细表头不完整或已变化')
        headers=list(headers)
        if len([h for h in headers if h in required])!=len(required):
            raise ValueError('原生出库明细表头重复')
        for line,values in enumerate(iterator,2):
            checkpoint(cancel)
            if not any(v is not None and v!='' for v in values):
                continue
            item=dict(zip(headers,values));io=identifier(item['出仓单号']);sku=identifier(item['商品编码'])
            if io not in documents or sku not in expected or (io,sku) in seen:
                raise ValueError('出库原件有范围外单据、商品或重复明细')
            if (identifier(item['内部订单号'])!=order['internal'] or identifier(item['线上订单号'])!=order['online']
                    or item['店铺名称']!=binding['shop'] or identifier(item['店铺编号'])!=binding['shop_id']
                    or item['状态']!='已出库'):
                raise ValueError('出库原件的原单身份或最终状态不符')
            qty=number(item['数量'],'出库数量');price=items[sku][1]
            if not qty.is_finite() or qty<0:
                raise ValueError('出库数量为空、负数或不明')
            amount=(qty*price).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
            seen.add((io,sku));doc_seen.add(io)
            by_doc[io]=by_doc.get(io,Decimal(0))+qty
            if sku not in rows:
                rows[sku]={'sku':sku,'name':str(item.get('名称') or ''),'quantity':str(qty),'amount':str(amount),
                           'currency':'CNY','price':str(price),'sheet':title,'row':line,'headers':headers,
                           'values':list(values),'ordered':str(expected[sku]),'outbound':io,'sources':[],
                           'price_basis':'同一内部订单完整原生商品成交价'}
            else:
                rows[sku]['quantity']=str(Decimal(rows[sku]['quantity'])+qty)
                rows[sku]['amount']=str(Decimal(rows[sku]['amount'])+amount)
            rows[sku]['sources'].append({'outbound':io,'sheet':title,'row':line,'quantity':str(qty),'amount':str(amount)})
    if set(rows)!=set(expected) or doc_seen!=set(documents):
        raise ValueError('出库原件未覆盖完整单据及原单商品，不能把缺行当0')
    if any(Decimal(row['quantity'])!=items[sku][0] for sku,row in rows.items()):
        raise ValueError('实配数量与同单最终成交明细数量不一致，需核对部分配货或多次出库')
    if any(Decimal(row['amount'])!=items[sku][2] for sku,row in rows.items()):
        raise ValueError('出库原件逐商品金额与同单成交明细不一致，需核对分批出库或分币差额')
    for io,doc in documents.items():
        if doc.get('quantity') is not None and by_doc[io]!=Decimal(doc['quantity']):
            raise ValueError('出库文件数量与原生出库单汇总不符')
    amount=sum((Decimal(row['amount']) for row in rows.values()),Decimal(0))
    if amount!=number(facts.get('amount'),'原生整单金额'):
        raise ValueError('商品金额与原生整单金额不一致；运费、优惠或部分配货须单独核对')
    return {'head':{k:p['head'][k] for k in HEAD_KEYS},'order_digest':order['digest'],'internal':order['internal'],
            'online':order['online'],'gid':order['gid'],'binding':binding,'native_status':'已发货','source_kind':KIND,
            'fields':['销售出库单.数量（已出库）','同单商品成交价×出库数量；逐商品、整单对账'],
            'file':{'sha':receipt['sha'],'name':'聚水潭实配_'+order['internal']+('.xlsx' if raw.startswith(b'PK') else '.xls')},
            'rows':list(rows.values()),'quantity':str(sum((Decimal(row['quantity']) for row in rows.values()),Decimal(0))),'amount':str(amount),
            'order_facts':facts,'outbounds':list(documents.values())}
