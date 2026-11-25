"""Reconcile cross-batch stock only where material work shares a warehouse SKU."""
from decimal import Decimal

import jst_auto_order_store as automatic
import jst_order_store as stock
import work_repository as wr


def source(catalog):
    return catalog.get('revision'), catalog.get('source_sha256')


def included(db, warehouse, baseline):
    result = set(baseline.get('included', ()))
    # The legacy write path records the exact generations its refreshed stock includes.
    current = stock.baseline(db, warehouse)
    if current and baseline['catalog'].get('source_sha256') and source(current['catalog']) == source(baseline['catalog']):
        result.update(current.get('included', ()))
    return result


def has_material(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='material_batches'").fetchone())


def is_material(db, oid):
    return bool(db.execute("SELECT 1 FROM normal_orders WHERE oid=? AND kind='material'", (oid,)).fetchone())


def shared_ledger(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name IN ('material_batches','special_packages') LIMIT 1").fetchone())


def _locally_unsubmitted(db, oid, warehouse, gid):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='special_records'").fetchone() and db.execute(
            "SELECT 1 FROM special_records WHERE object=? AND stage='原单' LIMIT 1",(oid,)).fetchone():
        return False
    if stock.locked(db, {'oid': oid, 'warehouse': warehouse}):
        return False
    if automatic.exists(db):
        task = db.execute('SELECT stage FROM jst_auto_orders WHERE gid=?', (gid,)).fetchone()
        if task and task[0] in automatic.RELEASED:
            return False
    if db.execute("SELECT 1 FROM jst_events WHERE oid=? AND warehouse=? AND action='导出聚水潭订单' "
                  "AND json_extract(detail,'$.gid')=? LIMIT 1", (oid, warehouse, gid)).fetchone():
        return False
    from lifecycle_store import has_formal_document
    return not any(has_formal_document(db, oid, warehouse, stage) for stage in ('payment', 'receipt'))


def cross_reservations(db, warehouse, baseline, skus, *, material=False, batch_id=None, exclude=()):
    """Return additional reservations and SKU-specific uncertainties. Caller owns the snapshot."""
    reserved, uncertain = {}, {}
    if not skus or not stock.exists(db) or not (material or shared_ledger(db)):
        return {'reserved': reserved, 'uncertain': uncertain}
    tables = {row[0] for row in db.execute('SELECT name FROM sqlite_master WHERE type=\'table\'')}
    has_batches = 'jst_generation_batches' in tables
    joins = ('LEFT JOIN jst_generation_batches gb ON gb.gid=a.gid' if has_batches else '')
    batch = 'gb.batch_id' if has_batches else 'NULL'
    sql = f'''SELECT a.gid,a.oid,r.sku,r.quantity,b.bid,{batch},n.number,n.kind
               FROM jst_active a JOIN jst_reservations r ON r.gid=a.gid
               JOIN jst_generations g ON g.gid=a.gid
               JOIN normal_orders n ON n.oid=a.oid
               LEFT JOIN jst_baselines b ON b.bid=json_extract(g.payload,'$.bid')
               {joins} WHERE a.warehouse=? AND r.sku IN (SELECT value FROM json_each(?))'''
    if not material:
        sql += " AND n.kind IN ('material','policy','opening')"
    included_gids = set(baseline.get('included', ()))
    excluded = included_gids | set(exclude)
    revision, sha = source(baseline['catalog'])
    local_only = {}

    def add(gid, sku, qty, old_revision, old_sha, reference, pending=False):
        if gid in excluded:
            return
        if (sha and (old_revision, old_sha) == (revision, sha)) or pending:
            reserved[sku] = reserved.get(sku, Decimal(0)) + Decimal(qty)
        else:
            uncertain.setdefault(sku, []).append(reference)

    origins = {None: (None,None)}
    releases = {}
    if 'special_releases' in tables:
        releases = {(gid,sku):Decimal(qty) for gid,sku,qty in db.execute(
            'SELECT gid,sku,qty FROM special_releases WHERE warehouse=?', (warehouse,))}
    for gid, oid, sku, qty, bid, other_batch, number, kind in db.execute(sql, (warehouse, wr.dumps(sorted(skus)))):
        if gid in excluded or batch_id is not None and other_batch == batch_id:
            continue
        if bid not in origins:
            origins[bid] = db.execute("SELECT json_extract(payload,'$.catalog.revision'),json_extract(payload,'$.catalog.source_sha256') FROM jst_baselines WHERE bid=?", (bid,)).fetchone()
        old_revision, old_sha = origins[bid]
        qty = Decimal(qty) - releases.get((gid,sku),0)
        if not qty:
            continue
        local = False
        if (old_revision, old_sha) != (revision, sha):
            if gid not in local_only:
                local_only[gid] = _locally_unsubmitted(db, oid, warehouse, gid)
            local = local_only[gid]
        add(gid, sku, qty, old_revision, old_sha, number, local)
    if 'material_retired_stock' in tables:
        sql = '''SELECT gid,sku,quantity,catalog_revision,source_sha,batch_id FROM material_retired_stock
                 WHERE warehouse=? AND sku IN (SELECT value FROM json_each(?))'''
        if not material:
            sql += " AND kind IN ('material','policy','opening')"
        for gid, sku, qty, old_revision, old_sha, parent in db.execute(sql, (warehouse, wr.dumps(sorted(skus)))):
            if batch_id is None or batch_id != parent:
                add(gid, sku, qty, old_revision, old_sha, '原执行 '+gid[:10])
    return {'reserved': {sku: format(qty, 'f') for sku, qty in sorted(reserved.items())},
            'uncertain': {sku: sorted(set(records)) for sku, records in sorted(uncertain.items())}}


def retain_retired(db, oid, warehouse, gid):
    """The deletion transaction can retain stock facts after a platform order existed."""
    from material_requests import eligibility
    from jst_batch_store import generation_batch

    row = db.execute('''SELECT n.kind,b.payload FROM jst_generations g JOIN normal_orders n ON n.oid=g.oid
        LEFT JOIN jst_baselines b ON b.bid=json_extract(g.payload,'$.bid') WHERE g.gid=? AND g.oid=?''', (gid, oid)).fetchone()
    if not row:
        return
    kind, raw = row
    if kind == 'material' and has_material(db):
        db.execute('UPDATE material_claims SET retired=1 WHERE oid=? AND warehouse=?', (oid, warehouse))
    catalog = wr.loads(raw)['catalog'] if raw else {}
    products = catalog.get('items', {})
    rows = [(sku, qty) for sku, qty in db.execute('SELECT sku,quantity FROM jst_reservations WHERE gid=?', (gid,))
            if kind == 'material' or not eligibility(products.get(sku))]
    if not rows:
        return
    db.execute('''CREATE TABLE IF NOT EXISTS material_retired_stock(
        gid TEXT NOT NULL,warehouse TEXT NOT NULL,sku TEXT NOT NULL,quantity TEXT NOT NULL,
        kind TEXT NOT NULL,catalog_revision TEXT,source_sha TEXT,batch_id TEXT,PRIMARY KEY(gid,sku))''')
    batch_id = generation_batch(db,gid)
    db.executemany('INSERT OR IGNORE INTO material_retired_stock VALUES(?,?,?,?,?,?,?,?)',
                   [(gid, warehouse, sku, qty, kind, *source(catalog), batch_id) for sku, qty in rows])
