"""Small host approval for one native request. No copied quantities or network work."""
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from aoxiang_jobs import encoded
from app_defaults import WAREHOUSES
from jst_auto_order_store import LEGACY_CANCELLATION, legacy_cancellation_history
from runtime_support import checkpoint
from sqlite_write import begin
import work_repository as wr


def _hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def identity(intent):
    return _hash(encoded({'basis': intent['basis'], 'identity': intent['identity'],
                          'mappingVersion': intent['mappingVersion'], 'fileSha256': intent['file']['sha256']}))


def facts(db, head):
    """Metadata only; Node repeats these fixed scoped queries at request release."""
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    def one(table, sql, args):
        row = db.execute(sql, args).fetchone() if table in tables else None
        return list(row) if row is not None else None
    oid, revision, warehouse = head['oid'], head['revision'], head['warehouse']
    parent = one('normal_orders', 'SELECT day,store,number,current FROM normal_orders WHERE oid=?', (oid,))
    managed = one('warehouse_execution_heads', 'SELECT 1 FROM warehouse_execution_heads WHERE oid=? LIMIT 1', (oid,)) is not None
    child = one('warehouse_execution_heads', 'SELECT source_revision,child_revision,state,epoch FROM warehouse_execution_heads WHERE oid=? AND warehouse=?', (oid, warehouse)) if managed else None
    signed = one('warehouse_execution_revisions', 'SELECT identity_digest,snapshot_digest FROM warehouse_execution_revisions WHERE oid=? AND warehouse=? AND source_revision=?', (oid, warehouse, revision)) if managed else one('normal_revisions', 'SELECT digest FROM normal_revisions WHERE oid=? AND revision=?', (oid, revision))
    versions = {s: one('lifecycle_versions', 'SELECT version,digest FROM lifecycle_versions WHERE oid=? AND revision=? AND warehouse=? AND stage=? ORDER BY version DESC LIMIT 1', (oid, revision, warehouse, s)) for s in ('return', 'operations', 'payment')}
    route = one('lifecycle_routes', 'SELECT version FROM lifecycle_routes WHERE oid=? AND revision=?', (oid, revision))
    legacy = None if versions['return'] else one('merchant_feedback', 'SELECT version,payload FROM merchant_feedback WHERE oid=? AND revision=? ORDER BY version DESC LIMIT 1', (oid, revision))
    if legacy is not None:
        legacy[1] = _hash(legacy[1])
    admission = one('store_admission', 'SELECT payload FROM store_admission WHERE store=?', (head['store'],))
    cancellation = one('jst_cancellations', '''SELECT online,successor,
        (SELECT max(id) FROM jst_cancellation_reviews WHERE cancelled_online=jst_cancellations.online)
        FROM jst_cancellations WHERE oid=? AND warehouse=? ORDER BY observed DESC,rowid DESC LIMIT 1''', (oid,warehouse))
    if cancellation is None:
        cancellation = one('jst_cancellation_holds', 'SELECT online FROM jst_cancellation_holds WHERE oid=? AND warehouse=?',
                           (oid,warehouse))
    if cancellation is None:
        cancellation = one('jst_auto_orders', 'SELECT online FROM jst_auto_orders WHERE oid=? AND warehouse=? AND '
                           +LEGACY_CANCELLATION, (oid,warehouse))
    if cancellation is None:
        history = legacy_cancellation_history(db, head, tables)
        if history:
            cancellation = [history[0]]
    return {'parent': parent[:3] if managed and parent else parent, 'managed': managed, 'child': child, 'signed': signed,
            'visibility': one('archive_visibility', 'SELECT deleted,token FROM archive_visibility WHERE oid=? AND warehouse=?', (oid, warehouse)),
            'deleted': one('deleted_order_tokens', 'SELECT operation_id FROM deleted_order_tokens WHERE oid=?', (oid,)),
            'retirement': one('warehouse_execution_retirements', 'SELECT operation_id FROM warehouse_execution_retirements WHERE oid=? AND warehouse=?', (oid, warehouse)),
            'operations': bool(versions['operations'] or route and route[0] >= 2), 'versions': versions, 'legacy': legacy,
            'admission': _hash(admission[0]) if admission else None,
            **({'jstCancellation': cancellation} if cancellation is not None else {})}


def authorize(path, intent, validate, *, cancel=None):
    """Full validation and scoped approval share a snapshot and short write transaction."""
    from warehouse_catalog import Repository
    path = Path(path).resolve()
    repository = Repository(path.parent)
    checkpoint(cancel)
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=0)) as db:
        db.execute('BEGIN')
        before = {w: repository.source_reference(w) for w in WAREHOUSES}
        evidence = validate(db)
        after = {w: repository.source_reference(w) for w in WAREHOUSES}
        if before != after:
            raise wr.Conflict('采购核验期间仓资料已变化，请重新核对；尚未上传')
        proof = {'schema': 1, 'day': evidence['context']['day'], 'facts': facts(db, intent['basis']),
                 'supplierFile': (path.parent / 'aoxiang' / 'suppliers.json').is_file(),
                 'catalogs': {w: {**before[w], 'revision': evidence['catalog_revisions'].get(w)} for w in WAREHOUSES}}
    # The expensive original validation above is read-only. The short write
    # section rechecks metadata, never decodes large signed/catalog trees.
    with closing(sqlite3.connect(path, timeout=0)) as db:
        begin(db, cancel)
        if facts(db,intent['basis'])!=proof['facts']:
            raise wr.Conflict('核验期间本仓付款或上游已变化；尚未上传，请重新核验')
        db.execute('CREATE TABLE IF NOT EXISTS purchase_dispatch_guards(purchase_id TEXT PRIMARY KEY,identity_hash TEXT NOT NULL,proof TEXT NOT NULL,state TEXT NOT NULL,request_hash TEXT,updated_at TEXT NOT NULL)')
        old = db.execute('SELECT identity_hash,state FROM purchase_dispatch_guards WHERE purchase_id=?', (intent['id'],)).fetchone()
        key = identity(intent)
        if old and (old[0] != key or old[1] not in ('AUTHORIZED', 'ABORTED')):
            raise ValueError('原采购已有提交登记或依据不同，请核对原任务；未重新上传')
        checkpoint(cancel)
        db.execute('INSERT INTO purchase_dispatch_guards VALUES(?,?,?,?,?,?) ON CONFLICT(purchase_id) DO UPDATE SET proof=excluded.proof,state=excluded.state,request_hash=NULL,updated_at=excluded.updated_at',
                   (intent['id'], key, encoded(proof), 'AUTHORIZED', None, wr.now()))
        db.commit()
