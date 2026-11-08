"""Freeze confirmed payment facts for AX procurement. Never writes lifecycle or model tables."""
import copy
import hashlib
from contextlib import closing
from decimal import Decimal

import order_lifecycle as lifecycle
import purchases
from purchase_io import code, number
from runtime_support import checkpoint
from aoxiang_jobs import encoded
from aoxiang_purchase_pricing import POLICY, priced_row


def fingerprint(plan):
    return hashlib.sha256(encoded({k: v for k, v in plan.items() if k != 'fingerprint'}).encode()).hexdigest()


def freeze_payment(path, head, configuration, suppliers, *, cancel=None):
    checkpoint(cancel)
    if not configuration.get('configured') or not configuration.get('enabled'):
        raise ValueError('请先核验翱象商家与门店')
    if head['revision'] != head['current_revision']:
        raise ValueError('只能以当前有效签发对应的已确认付款单生成采购文件')
    targets = [v for v in configuration['targets'] if v['buyerWarehouseCode'] == head['store']]
    if len(targets) != 1:
        raise ValueError('付款单门店编码未对应唯一已核验翱象门店，不按名称猜测')
    supplier = suppliers.get(head['warehouse'])
    if not isinstance(supplier, str) or not supplier or supplier != supplier.strip():
        raise ValueError('缺少本仓供应商编码或编码含首尾空格')
    code(supplier)
    with closing(purchases.connect(path)) as db:
        db.execute('BEGIN')
        purchases.validate_head(db, head)
        view = lifecycle.snapshot(path, head, cancel=cancel, db=db)
        if not view['health']['payment']:
            raise ValueError('付款单尚未确认或对照依据已过期，请先在生命周期核对付款单')
        payment = view['documents']['payment']
        if not payment:
            raise ValueError('尚无已确认付款单')
        rows, omitted = [], []
        seen = set()
        for index, row in enumerate(payment['payload']['rows']):
            checkpoint(cancel)
            sku = code(row['sku'])
            if sku in seen:
                raise ValueError('已确认付款快照存在重复商品编码：' + sku)
            seen.add(sku)
            qty = number(row['base_quantity'])
            amount = number(row['amount'], optional=True)
            if Decimal(qty) == 0:
                if amount is not None and Decimal(amount) != 0:
                    raise ValueError('付款数量为0但金额不为0，采购依据待核：' + sku)
                omitted.append({'index': index, 'sku': sku, 'quantity': qty, 'amount': amount, 'reason': row.get('quantity_origin')})
                continue
            if amount is None:
                raise ValueError('正数付款数量缺少商品金额，不能改用最新价格：' + sku)
            if row.get('currency', '') not in ('', 'CNY', '人民币', 'RMB'):
                raise ValueError('采购模板按元计价，付款币种需核对，不自动换汇：' + sku)
            rows.append({'index': index, 'sku': sku, 'name': row.get('name') or '', 'quantity': qty,
                         **priced_row(qty, amount), 'currency': row.get('currency', ''),
                         'source_quantity': row.get('quantity'), 'source_unit': row.get('unit'),
                         'source_factor': row.get('factor'), 'document_number': row.get('document_number'),
                         'sources': copy.deepcopy(row.get('sources', []))})
        if not rows:
            raise ValueError('此付款单没有正数付款数量，无需创建采购单')
        plan = {'schema': 1, 'operation': 'aoxiang.purchase.prepare',
                'head': {k: copy.deepcopy(head[k]) for k in ['oid', 'day', 'store', 'name', 'warehouse', 'number',
                          'revision', 'current_revision', 'digest', 'visibility_token']},
                'account_binding': copy.deepcopy(configuration['account_binding']),
                'store_mapping_version': configuration['store_mapping_version'],
                'platform_store': copy.deepcopy(targets[0]), 'supplier_code': supplier,
                'payment': copy.deepcopy(payment), 'rows': rows, 'omitted': omitted,
                'total_quantity': str(sum((Decimal(r['quantity']) for r in rows), Decimal(0))),
                'total_amount': str(sum((Decimal(r['amount']) for r in rows), Decimal(0))),
                'quantity_basis': 'confirmed_base_quantity_no_additional_conversion',
                'price_basis': POLICY,
                'payment_total_amount': str(sum((Decimal(r['payment_amount']) for r in rows), Decimal(0))),
                'rounding_delta': str(sum((Decimal(r['rounding_delta']) for r in rows), Decimal(0))),
                'create_external_order': False, 'post_creation_state': '待提交'}
    plan['fingerprint'] = fingerprint(plan)
    return plan


def validate_basis(path, plan, configuration, suppliers, *, cancel=None):
    checkpoint(cancel)
    if fingerprint(plan) != plan['fingerprint']:
        raise ValueError('采购预览内容变化，请重新生成')
    if (not configuration.get('enabled') or configuration['account_binding'] != plan['account_binding']
            or configuration['store_mapping_version'] != plan['store_mapping_version']
            or suppliers.get(plan['head']['warehouse']) != plan['supplier_code']):
        raise ValueError('翱象身份、门店绑定或供应商配置已变化，请重新核对')
    with closing(purchases.connect(path)) as db:
        db.execute('BEGIN')
        purchases.validate_head(db, plan['head'])
        view = lifecycle.snapshot(path, plan['head'], cancel=cancel, db=db)
        current = view['documents']['payment']
        if not current or (current['version'], current['digest']) != (plan['payment']['version'], plan['payment']['digest']):
            raise ValueError('付款单已更新，请重新预览，不能上传旧付款数量')
        if not view['health']['payment']:
            raise ValueError('付款单的回传对照依据变化，请先重新核对付款单')
    return True
