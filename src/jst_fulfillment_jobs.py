"""Select bounded original-order payment reads and optional historical sources."""
from contextlib import closing
from datetime import datetime, timedelta

import jst_auto_order_store as orders
from jst_order_confirmation import AWAITING
import jst_order_store as generations
from jst_shipping import read as read_shipments
from lifecycle_rules import JST_ORDER
import work_repository as wr
from sqlite_write import begin


_ACTIVE_ORDERS = '''FROM jst_auto_orders o
    JOIN jst_active a ON a.oid=o.oid AND a.warehouse=o.warehouse AND a.gid=o.gid
    JOIN jst_generations g ON g.gid=o.gid AND g.oid=o.oid AND g.warehouse=o.warehouse'''
_UNPAID = '''AND NOT EXISTS(SELECT 1 FROM lifecycle_versions v
    WHERE v.oid=o.oid AND v.revision=g.revision AND v.warehouse=o.warehouse AND v.stage='payment'
      AND v.version=(SELECT MAX(recent.version) FROM lifecycle_versions recent
         WHERE recent.oid=v.oid AND recent.revision=v.revision AND recent.warehouse=v.warehouse AND recent.stage=v.stage)
      AND COALESCE(json_extract(CAST(v.payload AS TEXT),'$.pending'),0)=0)'''


def enabled(path):
    with closing(wr.read_connection(path, timeout=.15)) as db:
        if not orders.exists(db, 'jst_fulfillment_preferences'):
            return False
        return db.execute('SELECT enabled FROM jst_fulfillment_preferences WHERE id=1').fetchone() == (1,)


def configure(path, value, progress=lambda _: None, cancel=None):
    if not isinstance(value, bool):
        raise ValueError('自动回流开关须明确开启或关闭')
    with closing(orders.connection(path)) as db, db:
        begin(db, cancel)
        db.execute('CREATE TABLE IF NOT EXISTS jst_fulfillment_preferences(id INTEGER PRIMARY KEY,enabled INTEGER NOT NULL)')
        db.execute('INSERT INTO jst_fulfillment_preferences VALUES(1,?) ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled', (int(value),))
    return value


def due(path, *, now=None, limit=10):
    cutoff = (now or datetime.fromisoformat(wr.now())) - timedelta(minutes=5)
    with closing(wr.read_connection(path, timeout=.15)) as db:
        db.execute('BEGIN')
        if not orders.exists(db) or not generations.exists(db):
            return []
        sql = "SELECT o.online " + _ACTIVE_ORDERS
        pending = orders.exists(db, 'jst_fulfillment_checks')
        if pending:
            sql += " LEFT JOIN jst_fulfillment_checks c ON c.online=o.online"
        sql += " WHERE o.internal IS NOT NULL AND o.stage IN ('AUDITED','REVIEW')"
        if pending:
            sql += " AND (c.online IS NULL OR (c.state IN ('WAIT_ALLOCATION','WAIT_CONNECTION') AND julianday(c.observed)<=julianday(?)))"
        if orders.exists(db, 'lifecycle_versions'):
            sql += ' ' + _UNPAID
        sql += " ORDER BY c.observed,o.online" if pending else " ORDER BY o.online"
        args = (cutoff.isoformat(),) if pending else ()
        return [row[0] for row in db.execute(sql + ' LIMIT ?', (*args, max(1, min(10, limit))))]


def waiting_review(path, *, include_paid=False):
    """Pending native candidates, including those retained across application restarts."""
    with closing(wr.read_connection(path, timeout=.15)) as db:
        db.execute('BEGIN')
        if not orders.exists(db) or not generations.exists(db) or not orders.exists(db, 'jst_fulfillment_checks'):
            return []
        sql = "SELECT o.online " + _ACTIVE_ORDERS + """
            JOIN jst_fulfillment_checks c ON c.online=o.online WHERE c.state='READY'"""
        if not include_paid and orders.exists(db, 'lifecycle_versions'):
            sql += ' ' + _UNPAID
        return [row[0] for row in db.execute(sql + " ORDER BY c.observed,o.online")]


def due_order_payment(path, *, now=None, limit=10):
    """Retry reads from verified originals, including orders imported by earlier versions."""
    cutoff = (now or datetime.fromisoformat(wr.now())) - timedelta(minutes=5)
    with closing(wr.read_connection(path, timeout=.15)) as db:
        db.execute('BEGIN')
        if not orders.exists(db) or not generations.exists(db):
            return []
        pending = orders.exists(db, 'jst_fulfillment_checks')
        sql = 'SELECT o.online ' + _ACTIVE_ORDERS
        if pending:
            sql += ' LEFT JOIN jst_fulfillment_checks c ON c.online=o.online'
        sql += " WHERE o.internal IS NOT NULL AND o.stage IN ('IMPORTED','AUDITED','AUDIT_UNKNOWN','REVIEW')"
        args = []
        if pending:
            # Older releases left valid imported orders in this state while waiting for audit.
            sql += """ AND (c.online IS NULL OR (julianday(c.observed)<=julianday(?) AND
                (c.state IN ('RETRY_PAYMENT_READ','WAIT_ALLOCATION','WAIT_CONNECTION')
                 OR (c.state='PAYMENT_SOURCE_PENDING' AND c.message=? AND o.stage IN ('IMPORTED','AUDIT_UNKNOWN'))"""
            args.extend((cutoff.isoformat(),f'聚水潭当前为{AWAITING}，请核对普通审核及原单'))
        has_lifecycle = orders.exists(db, 'lifecycle_versions')
        if pending and has_lifecycle:
            sql += """ OR (o.stage='AUDITED' AND c.state IN ('READY','NEEDS_REVIEW') AND NOT EXISTS(
                    SELECT 1 FROM lifecycle_versions WHERE oid=o.oid AND revision=g.revision
                    AND warehouse=o.warehouse AND stage=?))"""
            args.append(JST_ORDER)
        if pending:
            sql += ')))'
        if has_lifecycle:
            sql += ' ' + _UNPAID
        sql += ' ORDER BY c.observed,o.online' if pending else ' ORDER BY o.online'
        return [row[0] for row in db.execute(sql + ' LIMIT ?', (*args, max(1, min(10, limit))))]


def due_payment_locks(path, *, now=None, limit=10):
    """Recheck paid originals awaiting shipment on an already available connection."""
    cutoff = (now or datetime.fromisoformat(wr.now())) - timedelta(minutes=1)
    with closing(wr.read_connection(path, timeout=.15)) as db:
        db.execute('BEGIN')
        if not all(orders.exists(db, name) for name in ('jst_auto_orders', 'jst_active', 'jst_generations', 'lifecycle_versions')):
            return []
        checks = orders.exists(db, 'jst_fulfillment_checks')
        sql = '''SELECT o.online,o.oid,g.revision,o.warehouse,p.version,p.digest ''' + _ACTIVE_ORDERS + '''
            JOIN lifecycle_versions p ON p.oid=o.oid AND p.warehouse=o.warehouse AND p.revision=g.revision
                AND p.stage='payment' AND p.version=(SELECT MAX(v.version) FROM lifecycle_versions v
                    WHERE v.oid=p.oid AND v.revision=p.revision AND v.warehouse=p.warehouse AND v.stage=p.stage)
        '''
        if checks:
            sql += ' LEFT JOIN jst_fulfillment_checks c ON c.online=o.online'
        sql += ''' WHERE o.internal IS NOT NULL AND o.stage IN ('IMPORTED','AUDITED','AUDIT_UNKNOWN','REVIEW')
            AND COALESCE(json_extract(CAST(p.payload AS TEXT),'$.pending'),0)=0
            AND NOT EXISTS(SELECT 1 FROM lifecycle_versions r WHERE r.oid=p.oid AND r.revision=p.revision
                AND r.warehouse=p.warehouse AND r.stage='receipt'
                AND COALESCE(json_extract(CAST(r.payload AS TEXT),'$.pending'),0)=0)'''
        if checks:
            sql += ' AND (c.online IS NULL OR julianday(c.observed)<=julianday(?) OR p.saved>=c.observed)'
        sql += ' ORDER BY c.observed,o.online' if checks else ' ORDER BY p.saved,o.online'
        candidates = db.execute(sql, (cutoff.isoformat(),) if checks else ()).fetchall()
        if not candidates:
            return []
        observed = read_shipments(db, (row[1] for row in candidates))
        return [online for online, oid, revision, wh, version, digest in candidates
                if observed.get((oid, revision, wh), {}).get('payment_lock') != {'version':version, 'digest':digest}][:max(1, min(10, limit))]


def originals_for_scope(path, heads):
    with closing(wr.read_connection(path, timeout=.15)) as db:
        db.execute('BEGIN')
        keys = []
        for head in heads:
            row = db.execute('SELECT online FROM jst_auto_orders WHERE oid=? AND warehouse=? AND internal IS NOT NULL',
                             (head['oid'], head['warehouse'])).fetchone() if orders.exists(db) else None
            if row is None:
                raise ValueError(head['number'] + ' 尚未绑定聚水潭内部订单号')
            keys.append(row[0])
        return keys
