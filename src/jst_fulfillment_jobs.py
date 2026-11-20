"""Persisted opt-in and bounded due selection for allocation reads only."""
from contextlib import closing
from datetime import datetime, timedelta

import jst_auto_order_store as orders
import jst_order_store as generations
import work_repository as wr
from sqlite_write import begin


_ACTIVE_ORDERS = '''FROM jst_auto_orders o
    JOIN jst_active a ON a.oid=o.oid AND a.warehouse=o.warehouse AND a.gid=o.gid
    JOIN jst_generations g ON g.gid=o.gid AND g.oid=o.oid AND g.warehouse=o.warehouse'''
_UNPAID = '''AND NOT EXISTS(SELECT 1 FROM lifecycle_versions
    WHERE oid=o.oid AND revision=g.revision AND warehouse=o.warehouse AND stage='payment')'''


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
        sql += " WHERE o.internal IS NOT NULL AND o.stage IN ('IMPORTED','AUDITED','REVIEW')"
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
