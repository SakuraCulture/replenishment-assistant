"""Retain the imported order when its ordinary audit is verified."""
import jst_order_store as generations
import lifecycle_store as store
import work_repository as wr
from lifecycle_rules import DOCUMENT_SCHEMA, JST_ORDER


AUDITED = '已客审待财审'
AWAITING = '已付款待审核'
REVERSED = frozenset((AWAITING, '待付款', '已取消', '被合并', '被拆分'))


def record(db, task, status):
    if status != AUDITED and status not in REVERSED:
        return
    source = task['payload']
    head = source['head']
    old = store.decode(store.version_row(db, head, JST_ORDER)) if store.exists(db) else None
    reference = {'online': task['online'], 'internal': task['internal'], 'gid': task['gid'],
                 'order_digest': task['digest']}
    if status == AUDITED:
        if old and not old['payload']['pending'] and old['payload']['reference'] == reference:
            return
        if not task['internal'] or generations.active(db, head) != task['gid']:
            raise wr.Conflict('客审结果对应的仓单或生成版本已变化，请核对原单')
        generation, digest = generations.read_generation(db, task['gid'])
        if digest != source['generation']['digest']:
            raise wr.Conflict('客审结果对应的生成单摘要已变化，请核对原单')
        names = {row['sku']: row.get('name') for row in generation['rows']}
        payload = {
            'schema': DOCUMENT_SCHEMA, 'stage': JST_ORDER, 'pending': False,
            'head': {k: head[k] for k in ('oid','revision','warehouse','number','store','day','digest')},
            'basis': {'dependencies': generation['basis']['dependencies'], 'signed_digest': head['digest']},
            'reference': reference, 'native_status': status, 'files': [],
            'rows': [{'sku': sku, 'name': names.get(sku), 'base_quantity': qty} for sku, qty, _ in source['lines']],
        }
    else:
        if not old or old['payload']['pending'] or old['payload']['reference'] != reference:
            return
        payload = {**old['payload'], 'pending': True, 'native_status': status, 'rows': []}
    version = (old['version'] if old else 0) + 1
    store.schema(db)
    store.insert_version(db, head, JST_ORDER, version, store.fingerprint(payload), wr.dumps(payload).encode('utf-8'))
    action = '计入聚水潭已客审导入单' if status == AUDITED else '聚水潭导入单依据失效'
    detail = wr.dumps({'internal':task['internal'], 'native_status':status, 'rows':len(payload['rows'])})
    store.insert_event(db, wr.now(), head, JST_ORDER, version, action, detail)
