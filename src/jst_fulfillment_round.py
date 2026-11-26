"""One shared twelve-hour round of unattended original-order reads per business database."""
from contextlib import closing
import time

import jst_auto_order_store as orders
import jst_fulfillment_jobs as jobs
import performance_log
import work_repository as wr
from sqlite_write import begin


INTERVAL_MS = 12 * 60 * 60 * 1000


def now_ms():
    return time.time_ns() // 1_000_000


def _saved(db):
    if orders.exists(db, 'jst_fulfillment_round'):
        row = db.execute('SELECT next_at, pending FROM jst_fulfillment_round WHERE id=1').fetchone()
        if row:
            return row[0], wr.loads(row[1])
    return 0, []


def current(path, *, historical=True):
    with closing(wr.read_connection(path, timeout=.15)) as db:
        deadline, pending = _saved(db)
    if deadline > now_ms():
        return deadline, pending

    # Preserve priority and the ten-original cap. New arrivals wait for the next round.
    keys = {k: 'lock' for k in jobs.due_payment_locks(path, limit=5)}
    for key in jobs.due_order_payment(path):
        if len(keys) >= 10:
            break
        keys.setdefault(key, 'payment')
    if not keys and historical and jobs.enabled(path):
        keys = {k: 'historical' for k in jobs.due(path)}
    with closing(orders.connection(path)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        deadline, pending = _saved(db)
        now = now_ms()
        if deadline > now:
            return deadline, pending
        deadline = now + INTERVAL_MS
        db.execute('CREATE TABLE IF NOT EXISTS jst_fulfillment_round('
                   'id INTEGER PRIMARY KEY CHECK(id=1), next_at INTEGER NOT NULL, pending TEXT NOT NULL)')
        # Store a list to preserve selection order through JSON's sorted object keys.
        with performance_log.begin('jst_fulfillment.round_open'):
            db.execute('INSERT OR REPLACE INTO jst_fulfillment_round VALUES(1,?,?)',
                       (deadline, wr.dumps(list(keys.items()))))
        return deadline, list(keys.items())


def take(path, deadline, key, cancel):
    """Claim an original only after acquiring the browser. Never replay an interrupted read/export."""
    with closing(orders.connection(path)) as db, db:
        begin(db, cancel)
        saved, entries = _saved(db)
        pending = dict(entries)
        if saved != deadline or deadline <= now_ms() or key not in pending:
            return False
        kind = pending.pop(key)
        db.execute('UPDATE jst_fulfillment_round SET pending=? WHERE id=1',
                   (wr.dumps(list(pending.items())),))
    # The user may have confirmed payment, received the order or retired it during a wait.
    if kind == 'lock':
        return bool(jobs.due_payment_locks(path, online=key))
    if kind == 'historical':
        return jobs.enabled(path) and bool(jobs.due(path, online=key))
    return bool(jobs.due_order_payment(path, online=key))
