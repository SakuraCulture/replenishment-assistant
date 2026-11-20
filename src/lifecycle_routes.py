"""Persisted warehouse-document route, shared by write and read services."""
from lifecycle_stages import LEGACY, CURRENT, route
from order_keys import warehouse_revision as identity


def schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS lifecycle_routes(oid TEXT,revision INTEGER,version INTEGER NOT NULL,PRIMARY KEY(oid,revision))')


def required(db, head, staged=None,as_of=None):
    if staged and (*identity(head), 'operations') in staged:return True
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='lifecycle_routes'").fetchone():
        row=db.execute('SELECT version FROM lifecycle_routes WHERE oid=? AND revision=?',(head['oid'],head['revision'])).fetchone()
        if row and row[0]>=2:return True
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='lifecycle_versions'").fetchone():
        return bool(db.execute("SELECT 1 FROM lifecycle_versions WHERE oid=? AND revision=? AND warehouse=? AND stage='operations'"+(' AND julianday(saved)<=julianday(?)' if as_of else '')+' LIMIT 1',(*identity(head),*((as_of,) if as_of else ()))).fetchone())
    return False


def stages(db,head,staged=None,*,including=None,as_of=None):
    return route(including=='operations' or required(db,head,staged,as_of))


def demand_stage(db,head):return 'operations' if required(db,head) else 'return'
