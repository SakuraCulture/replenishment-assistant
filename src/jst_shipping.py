"""Read shipment status from verified observations of the original warehouse order."""
from jst_order_confirmation import DELIVERING, SHIPPED
from query_scope import oid_batches
from runtime_support import checkpoint


def read(db, oids, cancel=None):
    tables = {name for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'jst_auto_orders', 'jst_generations', 'jst_active', 'jst_events'} <= tables:
        return {}

    shipments = {}
    for marks, batch in oid_batches(oids):
        checkpoint(cancel)
        rows = db.execute(f"""SELECT o.oid,g.revision,o.warehouse,o.online,o.stage,a.gid,
            json_extract(e.detail,'$.native_status')
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
        for oid, revision, warehouse, online, stage, active, status in rows:
            key = oid, revision, warehouse
            if stage == 'CANCELLED':
                status = '已取消'
            ready = bool(active and (status == DELIVERING or status == SHIPPED and shipments.get(key, {}).get('ready')))
            shipments[key] = {'online': online, 'status': status, 'ready': ready}

        if 'jst_cancellations' in tables:
            for key in db.execute(f'SELECT oid,revision,warehouse FROM jst_cancellations WHERE oid IN ({marks})', batch):
                # An archived original can still have formal payment while the successor is being prepared.
                shipments.setdefault(key, {'online': None, 'status': '已取消', 'ready': False})
    return shipments
