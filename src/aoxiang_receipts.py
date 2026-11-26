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
from aoxiang_purchase import validate_basis, validate_original_context
from aoxiang_receipt_scope import PAYMENT_REVIEW, amount_review, review_stamp
import lifecycle_store
import work_repository as wr


class ReceiptPaymentReview(ValueError):
    code = 'RECEIPT_PAYMENT_REVIEW'


def checked_json(file, sha):
    raw = Path(file).read_bytes()
    import hashlib
    if hashlib.sha256(raw).hexdigest() != sha:
        raise ValueError('翱象收货原件或冻结依据已变化，请重新核对')
    return json.loads(raw)


def frozen_purchase(package):
    intent = checked_json(package['package_file'], package['package_sha256'])
    frozen = checked_json(package['plan_file'], package['plan_sha256'])
    if intent['id'] != package['id'] or intent['basis']['planFingerprint'] != frozen['fingerprint']:
        raise ValueError('采购本地索引与冻结付款依据不一致')
    return intent, frozen


def _posted_document(db, head, record):
    acknowledged = (record.get('hostReceipt') or {}).get('version') if record['state']=='SYNCED' else None
    document = lifecycle.current(db, head, 'receipt', version=acknowledged)
    if acknowledged is not None:
        return document
    # A later operator correction retains the exact receipt from which it was
    # made. Interrupted acknowledgement must refer to that original version.
    while document and (audit := document['payload'].get('manual_correction')):
        prior = audit['prior']
        if prior['version'] != document['version']-1:
            raise ValueError('实收更正缺少连续的原版本依据')
        document = lifecycle.current(db, head, 'receipt', version=prior['version'])
        if not document or document['digest'] != prior['digest']:
            raise ValueError('实收更正与留存的原单摘要不一致')
    return document


def existing_receipt(path, head, index, record, *, cancel=None):
    """Read back a posted receipt for acknowledgement only."""
    checkpoint(cancel)
    with closing(wr.read_connection(path)) as db:
        db.execute('BEGIN')
        from special_settlement import group_for_oid
        group=group_for_oid(db,head['oid'])
        if group:
            from special_native import posted_group
            return posted_group(db,head,index,record,group)
        old = _posted_document(db, head, record)
        if not old:
            return None
        error = '已保存的实收与本次原采购或收货原件无法唯一对应，请核对原任务。'
        if not index.is_file() or not record.get('manifest'):
            raise ValueError(error)
        intent, frozen = frozen_purchase(json.loads(index.read_text('utf-8')))
        signed = frozen['head']
        purchases.validate_head(db, signed)
        payload = old['payload']
        signed_fields = {k:signed[k] for k in ('oid','revision','warehouse','number','store','day','digest')}
        manifest_sha = record['manifest']['sha256']
        scope = record['scope']
        order_id = scope['orderId']
        if (payload.get('pending') or payload['stage'] != 'receipt'
                or payload['head'] != signed_fields
                or record.get('id',intent['id']) != intent['id'] or record['basis'] != intent['basis']
                or not order_id or scope.get('store',signed['store']) != signed['store']
                or scope.get('identity',intent['identity']) != intent['identity']
                or scope.get('supplier',intent['supplierCode']) != intent['supplierCode']):
            raise ValueError(error)
        # Use the observation retained when the business transaction committed.
        # The original download directory can be moved after successful posting.
        from hashlib import sha256
        attachment = lifecycle_store.file_row(db, manifest_sha)
        if not attachment or sha256(attachment[0]).hexdigest() != manifest_sha:
            raise ValueError(error)
        observation = validate_observation(json.loads(attachment[0]), intent)
        raw = payload.get('raw_rows') or ()
        sources = [f['sha256'] for f in observation['files']]
        if (observation['orderId'] != order_id or not raw
                or {f['sha'] for f in payload['files']} != {*sources,manifest_sha}
                or any(r.get('adapter') != 'aoxiang_actual_receipt'
                       or r.get('document_number') != order_id
                       or any((r.get('aoxiang_receipt') or {}).get(k)!=v for k,v in (
                           ('purchase_id',intent['id']),('order_id',order_id),('observation_sha',manifest_sha)))
                       for r in raw)):
            raise ValueError(error)
        return lifecycle_store.save_receipt(signed,'receipt',old['version'],False,old['digest'])


def verify_acknowledgement(record, original, receipt):
    saved = record.get('hostReceipt') or {}
    if (record['state'] != 'SYNCED' or not receipt
            or record.get('id') != original.get('id') or record.get('scope') != original.get('scope')
            or record.get('basis') != original.get('basis')
            or (record.get('manifest') or {}).get('sha256') != (original.get('manifest') or {}).get('sha256')
            or any(saved.get(k)!=receipt[k] for k in ('number','revision','stage','version'))):
        raise ValueError('未核实同一原采购与实收版本的确认回执，原待办保留。')


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


def _payment_review(change, intent, observation, manifest, old, approval):
    if not change:
        if approval:
            raise ValueError('付款版本或收货依据已变化，请重新打开原采购核对')
        return None
    stamp = review_stamp(change,intent['id'],observation['orderId'],manifest['sha256'])
    recorded = old['payload'].get('aoxiang_payment_review') if old else None
    if recorded and all(recorded.get(k)==v for k,v in stamp.items()):
        return recorded
    if old:
        raise ValueError('这份原采购实收已入账，请查看原实收和当前付款差额。')
    if not approval:
        raise ReceiptPaymentReview(PAYMENT_REVIEW)
    if approval.get('stamp') != stamp:
        raise ValueError('付款版本、采购关联或收货原件已变化，请重新打开核对')
    reviewer, basis = approval['reviewer'].strip(), approval['basis'].strip()
    if not reviewer or not basis:
        raise ValueError('请填写实收核对人和处理依据')
    from work_repository import now
    return {**stamp,'reviewer':reviewer,'basis':basis,'checked_at':now(),
            **{k:change[k] for k in ('old_amount','new_amount','difference')}}


def prepare(path, package, manifest, configuration, suppliers, *, approval=None, cancel=None, progress=lambda _: None):
    checkpoint(cancel)
    intent, frozen = frozen_purchase(package)
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
        original = {'version': frozen['payment']['version'], 'digest': frozen['payment']['digest']}
        change = amount_review(db, head, original)
        if change:
            validate_original_context(db, frozen, configuration, suppliers, cancel=cancel)
        else:
            validate_basis(path, frozen, configuration, suppliers, cancel=cancel, _db=db, for_binding=True)
        token = change['current'] if change else original
        if not basis['ready'] or basis['dependencies'].get('payment') != token:
            raise ValueError('付款或上游依据已变化，需先核对，不能自动同步实收')
        old = lifecycle.current(db, head, 'receipt')
        if old:
            old_raw = old['payload'].get('raw_rows', [])
            if (not old_raw or any(r.get('adapter') != 'aoxiang_actual_receipt' or r.get('aoxiang_receipt') != provenance for r in old_raw)):
                raise ValueError('已有人工收货或其他观察版本；自动同步不覆盖，请在原生命周期中核对校准')
        review = _payment_review(change, intent, o, manifest, old, approval)
        plan = lifecycle.make_plan(db, head, 'receipt', raw, files, aoxiang_payment_review=review)
    if plan['payload']['pending'] or plan['payload']['missing'] or plan['payload']['extra']:
        raise ValueError('收货与当前付款范围不一致；不能按缺行补0或自动接受清单外商品')
    return {'plans': [plan], 'files': files}


def synchronize(path, package, manifest, configuration, suppliers, *, approval=None, cancel=None, progress=lambda _: None):
    if package.get('intent',{}).get('openingGroup'):
        from special_native import sync_group
        return sync_group(path,package,manifest,configuration,suppliers)
    plan = prepare(path, package, manifest, configuration, suppliers, approval=approval, cancel=cancel, progress=progress)
    # Original service checks version, dependencies, files and cancellation under the write lock.
    return lifecycle.commit(path, plan, progress, cancel)[0]
