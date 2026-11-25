"""Record verified native order quantities apart from formal business payment."""
import jst_order_store as generations
import lifecycle_store as store
import work_repository as wr
from lifecycle_rules import DOCUMENT_SCHEMA, JST_ORDER


AUDITED = '已客审待财审'
AWAITING = '已付款待审核'
SHIPPING = ('发货中(打单拣货)', '已发货')
PAYMENT_STATES = frozenset((AWAITING, AUDITED, *SHIPPING))
REVERSED = frozenset(('待付款', '已取消', '被合并', '被拆分', '异常', '等供销商|外仓发货'))


def buyer_issue(receipt):
    status = receipt.get('buyer_check')
    if status == 'mismatch':
        return '回查的完整买家账号与原任务不同'
    if status == 'masked_conflict':
        return '聚水潭隐号的可见部分与原任务资料不符，原单身份待核'
    if status == 'unavailable':
        return '聚水潭未返回可核验的完整买家账号，原单身份待核'
    if status not in (None, 'verified') or receipt.get('contact_ok') is not True:
        return '聚水潭原单买家身份待核'
    return ''


def bound_cancelled_order(order, receipt):
    """An earlier verified internal number can anchor the native cancellation status."""
    binding = (order.get('proof') or {}).get('binding')
    internal = order.get('internal')
    return bool(binding and internal and receipt.get('found') is True
                and receipt.get('native_status') == '已取消' and receipt.get('buyer_check') == 'unavailable'
                and receipt.get('binding') == binding and receipt.get('online') == order['online']
                and receipt.get('internal') == internal and not receipt.get('detail_error'))


def record(db, task, status, *, rows=None):
    if not (status in PAYMENT_STATES and (rows is not None or status == AUDITED)) and status not in REVERSED:
        return
    source = task['payload']
    head = source['head']
    old = store.decode(store.version_row(db, head, JST_ORDER)) if store.exists(db) else None
    reference = {'online': task['online'], 'internal': task['internal'], 'gid': task['gid'],
                 'order_digest': task['digest']}
    if status in PAYMENT_STATES:
        if not task['internal'] or generations.active(db, head) != task['gid']:
            raise wr.Conflict('原订单对应的仓单或生成版本已变化，请核对原单')
        generation, digest = generations.read_generation(db, task['gid'])
        if digest != source['generation']['digest']:
            raise wr.Conflict('原订单对应的生成单摘要已变化，请核对原单')
        names = {row['sku']: row.get('name') for row in generation['rows']}
        quantities = [(r['sku'], r['quantity']) for r in rows] if rows is not None else [(sku, qty) for sku, qty, _ in source['lines']]
        items = [{'sku': sku, 'name': names.get(sku), 'base_quantity': qty} for sku, qty in quantities]
        if old and not old['payload']['pending'] and old['payload']['reference'] == reference and old['payload']['rows'] == items:
            return
        payload = {
            'schema': DOCUMENT_SCHEMA, 'stage': JST_ORDER, 'pending': False,
            'head': {k: head[k] for k in ('oid','revision','warehouse','number','store','day','digest')},
            'basis': {'dependencies': generation['basis']['dependencies'], 'signed_digest': head['digest']},
            'reference': reference, 'native_status': status, 'files': [],
            'rows': items,
        }
    else:
        if not old or old['payload']['pending'] or old['payload']['reference'] != reference:
            return
        payload = {**old['payload'], 'pending': True, 'native_status': status, 'rows': []}
    version = (old['version'] if old else 0) + 1
    store.schema(db)
    store.insert_version(db, head, JST_ORDER, version, store.fingerprint(payload), wr.dumps(payload).encode('utf-8'))
    action = '计入聚水潭已核实原单' if status in PAYMENT_STATES else '聚水潭导入单依据失效'
    detail = wr.dumps({'internal':task['internal'], 'native_status':status, 'rows':len(payload['rows'])})
    store.insert_event(db, wr.now(), head, JST_ORDER, version, action, detail)
