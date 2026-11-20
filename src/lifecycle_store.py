"""SQLite records for lifecycle versions, attachments, mappings and events.

Readers use the caller's snapshot. Writes enlist in the caller's transaction.
"""
import hashlib

import work_repository as wr
from lifecycle_rules import DOCUMENT_SCHEMA as SCHEMA
from order_keys import warehouse_revision as key


def schema(db):
    for sql in (
        'CREATE TABLE IF NOT EXISTS lifecycle_versions(oid TEXT,revision INTEGER,warehouse TEXT,stage TEXT,version INTEGER,saved TEXT,digest TEXT,payload TEXT,PRIMARY KEY(oid,revision,warehouse,stage,version))',
        'CREATE TABLE IF NOT EXISTS lifecycle_files(sha TEXT PRIMARY KEY,name TEXT,content BLOB)',
        'CREATE TABLE IF NOT EXISTS lifecycle_events(id INTEGER PRIMARY KEY,at TEXT,oid TEXT,revision INTEGER,warehouse TEXT,stage TEXT,version INTEGER,action TEXT,detail TEXT)',
        'CREATE TABLE IF NOT EXISTS lifecycle_mappings(stage TEXT,name TEXT,payload TEXT,PRIMARY KEY(stage,name))',
    ): db.execute(sql)

def exists(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE name='lifecycle_versions'").fetchone() is not None

def fingerprint(payload):
    # Only file paths are projected out. Nested business data is read-only here;
    # copying every raw cell doubled peak memory without changing the digest.
    value = dict(payload)
    value['files'] = [{k: f[k] for k in ('name', 'sha')} for f in value['files']]
    return hashlib.sha256(wr.dumps(value).encode()).hexdigest()


def decode(row):
    if not row: return None
    version, saved, digest, raw = row; payload = wr.loads(raw)
    if payload.get('schema') not in (1,SCHEMA) or fingerprint(payload) != digest: raise ValueError('生命周期单据快照校验失败')
    return {'version': version, 'saved': saved, 'digest': digest, 'payload': payload}

def version_row(db, head, stage, version=None, *, as_of=None):
    condition = ' AND version=?' if version is not None else ''
    args = (*key(head), stage, *((version,) if version is not None else ()))
    if as_of:condition+=' AND julianday(saved)<=julianday(?)';args=(*args,as_of)
    return db.execute('SELECT version,saved,digest,payload FROM lifecycle_versions WHERE oid=? AND revision=? AND warehouse=? AND stage=?' + condition + ' ORDER BY version DESC LIMIT 1', args).fetchone()


def version_rows(db, head, stage):
    return db.execute(
        'SELECT version,saved,digest,payload FROM lifecycle_versions WHERE oid=? AND revision=? AND warehouse=? AND stage=? ORDER BY version DESC',
        (*key(head), stage),
    )


def latest_version(db, head, stage):
    return db.execute(
        'SELECT version,digest FROM lifecycle_versions WHERE oid=? AND revision=? AND warehouse=? AND stage=? ORDER BY version DESC LIMIT 1',
        (*key(head), stage),
    ).fetchone()


def saved_at(db, head, stage, version):
    return db.execute(
        'SELECT saved FROM lifecycle_versions WHERE oid=? AND revision=? AND warehouse=? AND stage=? AND version=?',
        (*key(head), stage, version),
    ).fetchone()


def attachment_files(db,head,stage,doc):
    files={f['sha']:f for f in doc['payload']['files']}
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='lifecycle_events'").fetchone():return list(files.values())
    for raw, in db.execute('SELECT detail FROM lifecycle_events WHERE oid=? AND revision=? AND warehouse=? AND stage=? AND version=? AND action=? ORDER BY id',(*key(head),stage,doc['version'],'业务未变化，留存新原件')):
        for f in wr.loads(raw)['files']:files.setdefault(f['sha'],f)
    return list(files.values())


def file_row(db, sha):
    return db.execute('SELECT content FROM lifecycle_files WHERE sha=?', (sha,)).fetchone()


def retain_file(db, data, content):
    db.execute('INSERT OR IGNORE INTO lifecycle_files VALUES(?,?,?)', (data['sha'], data['name'], content))


def mappings(db, stage, reserved=()):
    return {
        name: wr.loads(payload)
        for name, payload in db.execute(
            'SELECT name,payload FROM lifecycle_mappings WHERE stage=? ORDER BY name', (stage,)
        )
        if stage != 'payment' or not name.startswith(reserved)
    }


def save_mapping(db, stage, name, value):
    db.execute(
        'INSERT INTO lifecycle_mappings VALUES(?,?,?) ON CONFLICT(stage,name) DO UPDATE SET payload=excluded.payload',
        (stage, name, wr.dumps(value)),
    )


def insert_version(db, head, stage, version, digest, content):
    # Keep the ledger's existing TEXT type. The spooled bytes are canonical UTF-8.
    db.execute(
        'INSERT INTO lifecycle_versions VALUES(?,?,?,?,?,?,?,CAST(? AS TEXT))',
        (*key(head), stage, version, wr.now(), digest, content),
    )


def event_exists(db, head, stage, version, action, detail):
    return db.execute(
        'SELECT 1 FROM lifecycle_events WHERE oid=? AND revision=? AND warehouse=? AND stage=? AND version=? AND action=? AND detail=?',
        (*key(head), stage, version, action, detail),
    ).fetchone() is not None


def insert_event(db, at, head, stage, version, action, detail):
    db.execute(
        'INSERT INTO lifecycle_events(at,oid,revision,warehouse,stage,version,action,detail) VALUES(?,?,?,?,?,?,?,?)',
        (at, *key(head), stage, version, action, detail),
    )
