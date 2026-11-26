"""Operator corrections to one current warehouse order and document stage."""
import hashlib
import uuid
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import jst_auto_order_store as jst_tasks
import jst_order_store
import lifecycle_store as store
import order_lifecycle as lifecycle
import purchases
import work_repository as wr
from jst_order_guards import saved_execution_reason
from platform_import_files import author
from purchase_io import code, number
from runtime_support import checkpoint


def _target(db, head, stage):
    if stage not in lifecycle.ALL_STAGES:
        raise ValueError('请选择需要更正的单据环节')
    if head['revision'] != head['current_revision']:
        raise wr.Conflict('当前打开的是历史签发，请打开本仓单的有效修订')
    purchases.validate_head(db, head)
    if stage == 'return':
        blocker = saved_execution_reason(db, head)
        if blocker:
            raise wr.Conflict(blocker+'。请在对应的下单、付款或收货环节更正')
    doc = lifecycle.current(db, head, stage)
    if not doc:
        raise ValueError('本环节尚无单据，请先登记实际单据')
    return doc


def _token(db, head, stage, doc):
    values = [doc['version'], doc['digest']]
    if stage != 'operations':
        values.extend((jst_order_store.active(db, head), jst_tasks.state(db, head)))
    if stage == 'payment':
        values.append(store.latest_version(db, head, 'receipt'))
    return hashlib.sha256(wr.dumps(values).encode()).hexdigest()


def _raw_rows(doc):
    return doc['payload'].get('raw_rows', doc['payload']['rows'])


def read(path, head, stage, progress=lambda _:None, cancel=None):
    checkpoint(cancel)
    head = lifecycle.full_head(path, head)
    with closing(purchases.connect(path)) as db:
        db.execute('BEGIN')
        doc = _target(db, head, stage)
        return {'token': _token(db, head, stage, doc), 'version': doc['version'],
                'rows': _raw_rows(doc), 'saved': doc['saved']}


def edits(changes, *, allow_blank=True):
    """Keep submitted change order, exact text identifiers and explicit deletes."""
    if not changes:
        raise ValueError('请至少登记一项更正')
    result = {}
    for index, row in enumerate(changes, 1):
        try:
            if not isinstance(row.get('sku'), str):
                raise ValueError('商品编码须按文本填写')
            sku = code(row['sku'])
            if sku in result:
                raise ValueError('商品编码重复：'+sku)
            action = row.get('action')
            if action == 'delete':
                result[sku] = {'action': action}
            elif action == 'set':
                values = {'action': action, 'quantity': number(row.get('quantity'), allow_blank)}
                if row.get('amount') not in (None, ''):
                    values['amount'] = number(row['amount'])
                if row.get('name'):
                    values['name'] = str(row['name']).strip()
                result[sku] = values
            else:
                raise ValueError('请选择设置数量或删除')
        except (ValueError, ArithmeticError) as exc:
            raise ValueError(f'第 {index} 条更正 · {exc}') from exc
    return result


def _comparison(row):
    if row is None:
        return None
    return {field: row.get(field) for field in ('name', 'quantity', 'base_quantity', 'unit', 'amount', 'currency')}


def _apply(raw, products, changes, head, stage, reference):
    rows = {row['sku']: dict(row) for row in raw}
    if len(rows) != len(raw):
        raise ValueError('原单商品编码重复，请先核对原件')
    differences = []
    for sku, edit in changes.items():
        before = rows.get(sku)
        if edit['action'] == 'delete':
            if before is None:
                raise ValueError('原单没有可删除的商品：'+sku)
            del rows[sku]
            after = None
        else:
            quantity = edit['quantity']
            product = products.get(sku, {})
            old = before or {}
            name = edit.get('name') or old.get('name') or product.get('name')
            if not name:
                raise ValueError('新增商品请填写商品名称：'+sku)
            amount = edit.get('amount', old.get('amount'))
            if stage == 'payment' and ('amount' not in edit and (
                    amount is None or Decimal(quantity or '0') != Decimal(old.get('base_quantity') or '0'))):
                raise ValueError('新增或改量的付款商品须填写实际金额：'+sku)
            document_number = old.get('document_number') or reference.get('document_number') or ''
            document_date = old.get('document_date') or reference.get('document_date')
            if stage in ('payment', 'receipt') and not document_number:
                raise ValueError('原单未留存外部单号，请先核对并登记实际单据')
            after = {'sku': sku, 'name': name, 'quantity': quantity, 'base_quantity': quantity,
                     'unit': '库存单位', 'factor': None, 'conversion_basis': None, 'amount': amount,
                     'currency': old.get('currency', reference.get('currency') or ''),
                     'signed_number': head['number'], 'store': head['store'], 'warehouse': head['warehouse'],
                     'revision': head['revision'], 'document_number': document_number, 'document_date': document_date,
                     'note': old.get('note'), 'raw_headers': [], 'raw_values': [], 'sources': [],
                     'quantity_origin': 'blank' if quantity is None else 'zero' if Decimal(quantity)==0 else 'value',
                     'adapter': 'manual_correction'}
            rows[sku] = after
        original, actual = _comparison(before), _comparison(after)
        if original != actual:
            differences.append({'sku': sku, 'action': edit['action'], 'before': original, 'after': actual})
    # The original allocated payment has its own immutable generation proof.
    # This explicitly approved correction retains the exact generation token.
    for row in rows.values():
        if row.get('adapter') == 'jst_allocated':
            row['adapter'] = 'manual_correction'
    return list(rows.values()), differences


def prepare(path, head, stage, expected, changes, operator, reason, evidence=None,
            progress=lambda _:None, cancel=None):
    checkpoint(cancel)
    audit = author(operator, reason)
    changed = edits(changes)
    files = []
    if evidence:
        source = Path(evidence).resolve(strict=True)
        with source.open('rb') as stream:
            sha = hashlib.file_digest(stream, 'sha256').hexdigest()
        files.append({'path': str(source), 'sha': sha, 'name': source.name})
    head = lifecycle.full_head(path, head)
    with closing(purchases.connect(path)) as db:
        db.execute('BEGIN')
        doc = _target(db, head, stage)
        if _token(db, head, stage, doc) != expected:
            raise wr.Conflict('本环节版本或原执行已变化，请重新读取')
        original = _raw_rows(doc)
        reference = original[0] if original else doc['payload'].get('manual_correction', {}).get('document_reference', {})
        raw, differences = _apply(original, head['products'], changed, head, stage, reference)
        if not differences:
            raise ValueError('本次没有商品或数量变化')
        plan = lifecycle.make_plan(db, head, stage, raw, files, explicit_zero=not raw)
        if plan['payload']['pending']:
            raise wr.Conflict('上游单据尚未就绪，请先核对本次适用的上游依据')
        metadata = {**audit, 'id': uuid.uuid4().hex, 'at': wr.now(), 'expected': expected,
                    'prior': {'version': doc['version'], 'digest': doc['digest']}, 'changes': differences,
                    'document_reference': {k: reference.get(k) for k in ('document_number', 'document_date', 'currency')},
                    'generation': jst_order_store.active(db, head), 'kind':head.get('kind','normal_model')}
        plan['payload']['manual_correction'] = metadata
        plan['payload'].pop('payment_source', None)
        plan['digest'] = store.fingerprint(plan['payload'])
        plan['changed'] = True
        plan.pop('source_evidence', None)
    return {'changes': differences, 'write': {'plans': [plan], 'files': files}}


def validate(db, plan):
    head, stage = plan['head'], plan['stage']
    doc = _target(db, head, stage)
    audit = plan['payload']['manual_correction']
    if _token(db, head, stage, doc) != audit['expected']:
        raise wr.Conflict('本环节版本或原执行已变化，请重新核对更正')
