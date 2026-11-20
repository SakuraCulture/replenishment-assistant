"""Immutable allocation originals and the latest observation, never paid facts."""
from hashlib import sha256

import jst_auto_order_store as orders
import work_repository as wr


def schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS jst_fulfillment_sources(
        online TEXT NOT NULL, digest TEXT NOT NULL, saved TEXT NOT NULL,
        payload TEXT NOT NULL, original BLOB NOT NULL,
        PRIMARY KEY(online,digest))''')
    db.execute('''CREATE TABLE IF NOT EXISTS jst_fulfillment_checks(
        online TEXT PRIMARY KEY, observed TEXT NOT NULL, state TEXT NOT NULL,
        digest TEXT, message TEXT NOT NULL)''')


def current(db, online):
    if not orders.exists(db, 'jst_fulfillment_checks'):
        return None
    row = db.execute('SELECT observed,state,digest,message FROM jst_fulfillment_checks WHERE online=?', (online,)).fetchone()
    return dict(zip(('observed', 'state', 'digest', 'message'), row)) if row else None


def source(db, online, digest):
    if not orders.exists(db, 'jst_fulfillment_sources'):
        raise ValueError('原实配资料不存在，请从原任务重新拉取')
    row = db.execute('SELECT payload,original FROM jst_fulfillment_sources WHERE online=? AND digest=?', (online, digest)).fetchone()
    if not row:
        raise ValueError('原实配资料不存在，请从原任务重新拉取')
    payload = wr.loads(row[0])
    raw = bytes(row[1])
    if orders.token(payload) != digest or sha256(raw).hexdigest() != payload['file']['sha']:
        raise ValueError('实配原件或校验摘要变化，停止付款准备')
    return payload, raw


def observe(db, online, state, message, digest=None):
    schema(db)
    db.execute('''INSERT INTO jst_fulfillment_checks VALUES(?,?,?,?,?)
        ON CONFLICT(online) DO UPDATE SET observed=excluded.observed,state=excluded.state,
        digest=excluded.digest,message=excluded.message''', (online, wr.now(), state, digest, message))


def save(db, online, payload, raw, message='实配资料已准备，等待人工核对付款'):
    if payload.get('online') != online or sha256(raw).hexdigest() != payload.get('file', {}).get('sha'):
        raise ValueError('实配原件与原单或摘要不一致，未保存候选')
    schema(db)
    digest = orders.token(payload)
    existing = db.execute('SELECT 1 FROM jst_fulfillment_sources WHERE online=? AND digest=?', (online, digest)).fetchone()
    if existing:
        source(db, online, digest)
    else:
        db.execute('INSERT INTO jst_fulfillment_sources VALUES(?,?,?,?,?)',
                   (online, digest, wr.now(), wr.dumps(payload), raw))
    observe(db, online, 'READY', message, digest)
    return digest


def history(db, online):
    if not orders.exists(db, 'jst_fulfillment_sources'):
        return []
    return db.execute('''SELECT saved,json_extract(payload,'$.source_kind'),
        json_extract(payload,'$.quantity'),json_extract(payload,'$.amount'),digest
        FROM jst_fulfillment_sources WHERE online=? ORDER BY saved DESC,rowid DESC''', (online,)).fetchall()
