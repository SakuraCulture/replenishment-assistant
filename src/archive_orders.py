"""Warehouse archive identities and reversible visibility, separate from snapshots.

No deletion of revisions, drafts, replies, source files or shared content chunks.
Tombstones apply to all revisions of one warehouse child; restore is explicit.
"""
from contextlib import closing
import sqlite3
from pathlib import Path
import work_repository as wr
from app_defaults import WAREHOUSES
from runtime_support import checkpoint
from warehouse_orders import child_number

LEGACY='旧版未标仓'


def warehouse(d):
    if d.get('_source')=='地采商品':return '地采'
    return d.get('AU') if d.get('AU') in WAREHOUSES else LEGACY


def scopes(rows):
    found={warehouse(d) for d in rows}
    return [w for w in (*WAREHOUSES,LEGACY) if w in found]


def number(parent,wh):
    return child_number(parent,wh) if wh in WAREHOUSES else parent+'-LEGACY'


def states(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='archive_visibility'").fetchone():return {}
    return {(oid,wh):{'deleted':bool(deleted),'token':token,'at':at} for oid,wh,deleted,token,at in db.execute('SELECT oid,warehouse,deleted,token,at FROM archive_visibility')}


def require_active(db,oid,wh=None):
    if any(o==oid and (wh is None or w==wh) and s['deleted'] for (o,w),s in states(db).items()):
        raise wr.Conflict('此仓正常单已在回收站，请先恢复再签发、导出或导入回传；原快照仍保留')


def change(path,head,deleted,progress=lambda _:None,cancel=None):
    """Compare selected current revision and visibility token inside write lock."""
    wh=head.get('warehouse')
    if wh not in (*WAREHOUSES,LEGACY):raise ValueError('请先选择具体仓单')
    checkpoint(cancel)
    import storage_chunks
    storage_chunks.backup_before_write(Path(path),progress)
    with closing(sqlite3.connect(path,timeout=10)) as db,db:
        db.execute('BEGIN IMMEDIATE');checkpoint(cancel)
        current=db.execute('SELECT current FROM normal_orders WHERE oid=?',(head['oid'],)).fetchone()
        signed=db.execute('SELECT digest FROM normal_revisions WHERE oid=? AND revision=?',(head['oid'],head['revision'])).fetchone()
        if not current or current[0]!=head.get('current_revision',head['revision']) or not signed or signed[0]!=head['digest']:
            raise wr.Conflict('归档修订已变化，请刷新后重试')
        old=states(db).get((head['oid'],wh),{'deleted':False,'token':0})
        if old['token']!=head.get('visibility_token',0) or old['deleted']==bool(deleted):
            raise wr.Conflict('回收站状态已变化，请刷新后重试')
        db.execute('CREATE TABLE IF NOT EXISTS archive_visibility(oid TEXT NOT NULL,warehouse TEXT NOT NULL,deleted INTEGER NOT NULL,token INTEGER NOT NULL,at TEXT NOT NULL,PRIMARY KEY(oid,warehouse))')
        stamp=wr.now();token=old['token']+1
        db.execute('INSERT INTO archive_visibility VALUES(?,?,?,?,?) ON CONFLICT(oid,warehouse) DO UPDATE SET deleted=excluded.deleted,token=excluded.token,at=excluded.at',(head['oid'],wh,int(deleted),token,stamp))
        detail={'warehouse':wh,'child_number':head['number'],'all_revisions':True,'visibility_token':token}
        db.execute('INSERT INTO normal_events(at,oid,revision,action,detail) VALUES(?,?,?,?,?)',(stamp,head['oid'],head['revision'],'移入回收站' if deleted else '恢复归档',wr.dumps(detail)))
        checkpoint(cancel)
    return {'deleted':bool(deleted),'token':token,'warehouse':wh}
