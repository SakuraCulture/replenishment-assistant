"""Validated AX observations -> existing full-document receipt service.

The component owns native file validation; this adapter independently checks every
business field against the frozen payment. Originals and the exact observation are
retained by lifecycle.commit. It never updates model/signed/payment records.
"""
import json
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import order_lifecycle as lifecycle
import purchases
from purchase_io import number, code
from runtime_support import checkpoint
from aoxiang_directory import file_hash
from aoxiang_purchase import validate_basis


def checked_json(file, sha):
    raw = Path(file).read_bytes()
    import hashlib
    if hashlib.sha256(raw).hexdigest() != sha:
        raise ValueError('翱象收货原件或冻结依据已变化，请重新核对')
    return json.loads(raw)


def validate_observation(observation, intent):
    o, p = observation, intent
    if (o.get('schema') != 1 or o.get('operation') != 'purchase.receipt.observation'
            or o.get('purchaseId') != p['id'] or o.get('identity') != p['identity'] or o.get('basis') != p['basis']
            or o.get('supplierCode') != p['supplierCode'] or not o.get('orderId')
            or o.get('quantityBasis') != 'native_actual_inventory_quantity_no_conversion'):
        raise ValueError('收货快照与采购付款依据不一致')
    if not o.get('eligible') or not o.get('terminal') or o.get('issues'):
        raise ValueError('尚无完整且无冲突的已完成收货快照，不能记作正式实收')
    main, rows = o['main'], o['rows']
    expected = {r['sku']: r for r in p['rows']}
    if len(rows) != len(expected) or len({r['sku'] for r in rows}) != len(expected) or {r['sku'] for r in rows} != set(expected):
        raise ValueError('收货商品缺失、重复或超出原采购范围；不能按缺行补0')
    for r in [main, *rows]:
        if r['orderId'] != o['orderId'] or r['status'] != '已完成' or r['store'] != p['basis']['store'] or r['supplier'] != p['supplierCode']:
            raise ValueError('主子单单号、状态、门店或供应商不一致')
    if Decimal(number(main['skuCount'])) != len(rows):
        raise ValueError('主子单商品种数不一致')
    totals = {'quantity': Decimal(0), 'amount': Decimal(0), 'receivedQuantity': Decimal(0), 'receivedAmount': Decimal(0)}
    for r in rows:
        code(r['sku'])
        if not isinstance(r.get('_sourceRow'), int) or r['_sourceRow'] < 2 or not str(r.get('unit') or '').strip():
            raise ValueError('收货来源行号或基础单位缺失')
        for k in totals:
            value = Decimal(number(r[k]))  # Empty is unknown, never zero.
            totals[k] += value
            if k in ('quantity', 'amount') and value != Decimal(expected[r['sku']][k]):
                raise ValueError('平台采购数量或金额已改变，不能自动覆盖原付款依据：' + r['sku'])
    for k, total in totals.items():
        if total != Decimal(number(main[k])):
            raise ValueError('主子单数量金额合计不一致：' + k)
    if totals['quantity'] != Decimal(p['totalQuantity']) or totals['amount'] != Decimal(p['totalAmount']):
        raise ValueError('采购合计与冻结付款依据不一致')
    if [f['source'] for f in o['files']] != ['D16', 'D17']:
        raise ValueError('收货主子原件缺失或顺序改变')
    return o


def prepare(path, package, manifest, configuration, suppliers, *, cancel=None, progress=lambda _: None):
    checkpoint(cancel)
    intent = checked_json(package['package_file'], package['package_sha256'])
    frozen = checked_json(package['plan_file'], package['plan_sha256'])
    if intent['id'] != package['id'] or intent['basis']['planFingerprint'] != frozen['fingerprint']:
        raise ValueError('采购本地索引与冻结付款依据不一致')
    validate_basis(path, frozen, configuration, suppliers, cancel=cancel)
    o = validate_observation(checked_json(manifest['file'], manifest['sha256']), intent)
    files = []
    for f in o['files']:
        checkpoint(cancel)
        if file_hash(f['file']) != f['sha256']:
            raise ValueError('翱象采购原件已变化：' + Path(f['file']).name)
        files.append({'path': f['file'], 'sha': f['sha256'], 'name': Path(f['file']).name})
    files.append({'path': manifest['file'], 'sha': manifest['sha256'], 'name': Path(manifest['file']).name})
    detail = o['files'][1]
    provenance = {'purchase_id': intent['id'], 'order_id': o['orderId'], 'observation_sha': manifest['sha256'],
                  'observed_at': o['observedAt'], 'platform_completed_at': o.get('platformCompletedAt'),
                  'quantity_basis': o['quantityBasis']}
    raw = []
    for r in o['rows']:
        checkpoint(cancel)
        qty = number(r['receivedQuantity'])
        raw.append({'sku': r['sku'], 'name': r['name'], 'quantity': qty, 'base_quantity': qty,
                    'unit': '库存单位', 'source_unit': r['unit'], 'factor': None, 'conversion_basis': None,
                    'amount': number(r['receivedAmount']), 'currency': 'CNY',
                    'document_number': o['orderId'], 'document_date': None,
                    'note': '翱象已完成实收；平台未提供完成时间，观察时间另存，不推测收货日期',
                    'sources': [{'sha': detail['sha256'], 'file': Path(detail['file']).name, 'sheet': '采购子单详情', 'row': r['_sourceRow']}],
                    'raw_headers': list(r), 'raw_values': list(r.values()),
                    'adapter': 'aoxiang_actual_receipt', 'aoxiang_receipt': provenance})
    head = lifecycle.full_head(path, frozen['head'])
    with closing(purchases.connect(path)) as db:
        db.execute('BEGIN')
        purchases.validate_head(db, head)
        basis = lifecycle.upstream(db, head, 'receipt')
        token = {'version': frozen['payment']['version'], 'digest': frozen['payment']['digest']}
        if not basis['ready'] or basis['dependencies']['payment'] != token:
            raise ValueError('付款或上游依据已变化，需先核对，不能自动同步实收')
        old = lifecycle.current(db, head, 'receipt')
        if old:
            old_raw = old['payload'].get('raw_rows', [])
            if (not old_raw or any(r.get('adapter') != 'aoxiang_actual_receipt' or r.get('aoxiang_receipt') != provenance for r in old_raw)):
                raise ValueError('已有人工收货或其他观察版本；自动同步不覆盖，请在原生命周期中核对校准')
        plan = lifecycle.make_plan(db, head, 'receipt', raw, files)
    if plan['payload']['pending'] or plan['payload']['missing'] or plan['payload']['extra']:
        raise ValueError('收货与当前付款范围不一致；不能按缺行补0或自动接受清单外商品')
    return {'plans': [plan], 'files': files}


def synchronize(path, package, manifest, configuration, suppliers, *, cancel=None, progress=lambda _: None):
    plan = prepare(path, package, manifest, configuration, suppliers, cancel=cancel, progress=progress)
    # Original service checks version, dependencies, files and cancellation under the write lock.
    return lifecycle.commit(path, plan, progress, cancel)[0]
