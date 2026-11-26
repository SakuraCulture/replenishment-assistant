"""Storage for 聚水潭 allocation snapshots, active generations and locks.

Mutations use the connection and transaction owned by the calling service.
"""
import hashlib
import sqlite3
from contextlib import closing

import work_repository as wr
from runtime_support import checkpoint


def digest(value):
    return hashlib.sha256(wr.dumps(value).encode()).hexdigest()


def exists(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='jst_generations'").fetchone())


def schema(db):
    for sql in (
        'CREATE TABLE IF NOT EXISTS jst_baselines(bid TEXT PRIMARY KEY,warehouse TEXT NOT NULL,created TEXT NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS jst_warehouses(warehouse TEXT PRIMARY KEY,bid TEXT NOT NULL,serial INTEGER NOT NULL)',
        'CREATE TABLE IF NOT EXISTS jst_generations(gid TEXT PRIMARY KEY,operation TEXT UNIQUE NOT NULL,oid TEXT NOT NULL,revision INTEGER NOT NULL,warehouse TEXT NOT NULL,day TEXT NOT NULL,saved TEXT NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS jst_active(oid TEXT,warehouse TEXT,gid TEXT NOT NULL,PRIMARY KEY(oid,warehouse))',
        'CREATE TABLE IF NOT EXISTS jst_headers(gid TEXT PRIMARY KEY,payload TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS jst_locks(oid TEXT,warehouse TEXT,gid TEXT NOT NULL,locked INTEGER NOT NULL,at TEXT NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(oid,warehouse))',
        'CREATE TABLE IF NOT EXISTS jst_reservations(gid TEXT,sku TEXT,quantity TEXT NOT NULL,PRIMARY KEY(gid,sku))',
        'CREATE TABLE IF NOT EXISTS jst_allocation_rounds(oid TEXT,warehouse TEXT,revision INTEGER,token TEXT NOT NULL,payment_version INTEGER NOT NULL,receipt_version INTEGER NOT NULL,PRIMARY KEY(oid,warehouse))',
        'CREATE TABLE IF NOT EXISTS jst_events(id INTEGER PRIMARY KEY,at TEXT,day TEXT,oid TEXT,warehouse TEXT,action TEXT,detail TEXT)',
        'CREATE TABLE IF NOT EXISTS jst_files(sha TEXT PRIMARY KEY,name TEXT,content BLOB)',
        'CREATE TABLE IF NOT EXISTS jst_runs(runid TEXT PRIMARY KEY,created TEXT,payload TEXT,digest TEXT)',
        'CREATE TABLE IF NOT EXISTS jst_steps(operation TEXT PRIMARY KEY,runid TEXT NOT NULL,result TEXT NOT NULL)',
        'CREATE INDEX IF NOT EXISTS jst_event_day ON jst_events(day,id)',
        'CREATE INDEX IF NOT EXISTS jst_generation_order ON jst_generations(oid,warehouse)',
    ):
        db.execute(sql)


def state(db, warehouse):
    if not exists(db):
        return None
    return db.execute('SELECT bid,serial FROM jst_warehouses WHERE warehouse=?', (warehouse,)).fetchone()


def active(db, head):
    if not exists(db):
        return None
    row = db.execute('SELECT gid FROM jst_active WHERE oid=? AND warehouse=?', (head['oid'], head['warehouse'])).fetchone()
    return row[0] if row else None


def locked(db, head):
    if not exists(db):
        return None
    row = db.execute('SELECT gid FROM jst_locks WHERE oid=? AND warehouse=? AND locked=1', (head['oid'], head['warehouse'])).fetchone()
    return row[0] if row else None


def allocation_round(db, head):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='jst_allocation_rounds'").fetchone():
        return None
    return db.execute(
        'SELECT token,payment_version,receipt_version FROM jst_allocation_rounds WHERE oid=? AND warehouse=? AND revision=?',
        (head['oid'],head['warehouse'],head['revision'])).fetchone()


def list_generation_state(db, batches, cancel=None):
    """Return active generation headers and locks for the selected parent batches."""
    current = {}
    locks = {}
    if exists(db):
        for placeholders, oids in batches:
            checkpoint(cancel)
            current.update(
                ((oid, warehouse), (gid, wr.loads(payload)))
                for oid, warehouse, gid, payload in db.execute(
                    'SELECT a.oid,a.warehouse,a.gid,h.payload FROM jst_active a '
                    'JOIN jst_headers h ON a.gid=h.gid WHERE a.oid IN ('+placeholders+')', oids)
            )
            locks.update(
                ((oid, warehouse), gid)
                for oid, warehouse, gid in db.execute(
                    'SELECT oid,warehouse,gid FROM jst_locks WHERE locked=1 AND oid IN ('+placeholders+')', oids)
            )
    return current, locks


def has_generation(db, oid):
    return exists(db) and db.execute('SELECT 1 FROM jst_active WHERE oid=?', (oid,)).fetchone()


def event(db, head, action, detail):
    db.execute('INSERT INTO jst_events(at,day,oid,warehouse,action,detail) VALUES(?,?,?,?,?,?)',
               (wr.now(), head.get('day', wr.today()), head.get('oid'), head.get('warehouse'), action, wr.dumps(detail)))


def decode(row):
    if not row:
        return None
    value = wr.loads(row[0])
    if digest(value) != row[1]:
        raise ValueError('聚水潭快照校验失败')
    return value


def read_generation(db, gid):
    if not exists(db):
        raise ValueError('没有已保存生成单')
    row = db.execute('SELECT payload,digest FROM jst_generations WHERE gid=?', (gid,)).fetchone()
    value = decode(row)
    if value is None:
        raise ValueError('生成单不存在')
    if value.get('gid') != gid:
        raise ValueError('生成记录标识与快照不一致')
    return value, row[1]


def get(path, gid, progress=lambda _: None, cancel=None):
    checkpoint(cancel)
    with closing(sqlite3.connect(path)) as db:
        return read_generation(db, gid)[0]


def baseline(db, warehouse):
    current = state(db, warehouse)
    if current is None:
        return None
    return decode(db.execute('SELECT payload,digest FROM jst_baselines WHERE bid=?', (current[0],)).fetchone())
