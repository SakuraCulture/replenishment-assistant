"""Derive receipt readiness from verified shipment and exact saved payment evidence."""
from jst_order_confirmation import DELIVERING, SHIPPED
from jst_order_store import event
from order_keys import warehouse_revision
from query_scope import oid_batches
from runtime_support import checkpoint
import work_repository as wr


def read(db, oids, cancel=None):
    tables = {name for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'jst_auto_orders', 'jst_generations', 'jst_active', 'jst_events'} <= tables:
        return {}

    shipments = {}
    for marks, batch in oid_batches(oids):
        checkpoint(cancel)
        rows = db.execute(f"""SELECT o.oid,g.revision,o.warehouse,o.online,o.stage,a.gid,
            json_extract(e.detail,'$.native_status'),json_type(e.detail,'$.payment_lock'),
            json_extract(e.detail,'$.payment_lock')
            FROM jst_auto_orders o
            JOIN jst_generations g ON g.gid=o.gid AND g.oid=o.oid AND g.warehouse=o.warehouse
            LEFT JOIN jst_active a ON a.gid=o.gid AND a.oid=o.oid AND a.warehouse=o.warehouse
            LEFT JOIN jst_events e ON e.day=g.day AND e.oid=o.oid AND e.warehouse=o.warehouse
                AND e.action IN ('聚水潭原单核对','聚水潭回读原单状态') AND json_valid(e.detail)
                AND json_extract(e.detail,'$.gid')=o.gid AND json_extract(e.detail,'$.online')=o.online
                AND json_extract(e.detail,'$.internal')=o.internal
                AND json_extract(e.detail,'$.native_status') IS NOT NULL
            WHERE o.oid IN ({marks}) AND o.stage NOT IN ('PREPARED','PREVIEWED')
            ORDER BY e.at,e.id""", batch)
        for oid, revision, warehouse, online, stage, active, status, lock_type, lock_value in rows:
            key = oid, revision, warehouse
            prior = shipments.get(key, {})
            if stage == 'CANCELLED':
                status = '已取消'
            observed = bool(active and (status == DELIVERING or status == SHIPPED and prior.get('observed')))
            lock = prior.get('payment_lock') if observed else None
            if observed and lock_type is not None:
                lock = wr.loads(lock_value) if lock_type == 'object' else None
            shipments[key] = {'online': online, 'status': status, 'observed': observed, 'payment_lock': lock}

        if 'jst_cancellations' in tables:
            for key in db.execute(f'SELECT oid,revision,warehouse FROM jst_cancellations WHERE oid IN ({marks})', batch):
                # An archived original can still have formal payment while the successor is being prepared.
                shipments.setdefault(key, {'online': None, 'status': '已取消', 'observed': False, 'payment_lock': None})
    return shipments


def observe_payment_read(db, order, status, *, payment_checked, matched_payment, source_digest):
    """Bind the saved version once, in the same transaction as verified native facts."""
    head = order['payload']['head']
    detail = {'online': order['online'], 'gid': order['gid'], 'internal': order['internal'], 'native_status': status}
    if payment_checked and status in (DELIVERING, SHIPPED):
        prior = read(db, [head['oid']]).get(warehouse_revision(head), {})
        if (status == DELIVERING or prior.get('observed')) and matched_payment != prior.get('payment_lock'):
            detail.update(payment_lock=matched_payment, payment_source_digest=source_digest)
    event(db, head, '聚水潭回读原单状态', detail)
    return bool(detail.get('payment_lock'))
