"""Expose exact platform acknowledgements to the existing payment recognizer.

This is a read-only projection. It grants neither payment confirmation nor an
alias for every order from the same shop. Conflicting original links fail closed.
"""
import re

import jst_auto_order_store as orders
import work_repository as wr
from payment_evidence import HEAD_KEYS, mapping_key, same_head


def merge(db, saved):
    result = dict(saved)
    if not orders.exists(db):
        return result
    for online, in db.execute('SELECT online FROM jst_auto_orders WHERE internal IS NOT NULL ORDER BY online'):
        order = orders.read(db, online)
        payload = order['payload']
        binding = (order['proof'] or {}).get('binding') or {}
        head = payload['head']
        internal = order['internal']
        if order['stage'] not in orders.RELEASED or not re.fullmatch(r'[1-9]\d*', internal or ''):
            continue
        if (binding.get('warehouse') != head['warehouse'] or binding.get('shop') != payload['shop']['name']
                or not all(binding.get(k) for k in ('company', 'coid', 'uid', 'shop_id'))
                or not re.fullmatch(r'[1-9]\d*', str(binding['coid']))):
            continue
        key = mapping_key('order', str(binding['coid']), internal)
        value = {'source_scope': str(binding['coid']), 'internal': internal,
                 'original_name': payload['shop']['name'], 'head': {k: head[k] for k in HEAD_KEYS},
                 'generation_id': order['gid'], 'source': 'jst_auto_order'}
        old = result.get(key)
        if old and (not same_head(old.get('head', {}), head)
                    or old.get('generation_id') not in (None, order['gid'])
                    or old.get('original_name') != value['original_name']):
            raise wr.Conflict('聚水潭内部单号的自动下单关联与付款原关联冲突，请核对原单')
        result.setdefault(key, value)
    return result
