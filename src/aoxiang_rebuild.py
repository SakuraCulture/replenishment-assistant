"""Read only selected order identities for an explicit purchase rebuild."""
import sqlite3
from contextlib import closing
from pathlib import Path
from runtime_support import checkpoint
from warehouse_orders import child_number
from deletion_barrier import require


def current_heads(path,items,progress=lambda _:None,cancel=None):
    heads=[]
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('BEGIN')
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for item in items:
            checkpoint(cancel);basis=item.get('head') or item.get('basis') or {};oid=basis.get('oid');wh=basis.get('warehouse')
            require(db,oid)
            row=db.execute('SELECT day,store,number,current FROM normal_orders WHERE oid=?',(oid,)).fetchone()
            if not row or wh not in ('山东仓','河北仓') or row[1]!=basis.get('store') or child_number(row[2],wh)!=basis.get('number'):
                raise ValueError('原仓单已删除或身份不符，不能按同号新单重建')
            visible=db.execute('SELECT deleted,token FROM archive_visibility WHERE oid=? AND warehouse=?',(oid,wh)).fetchone() if 'archive_visibility' in tables else None
            if visible and visible[0]:raise ValueError('原仓单已移入回收站，请先在订单维护确认原单状态')
            digest=db.execute('SELECT digest FROM normal_revisions WHERE oid=? AND revision=?',(oid,row[3])).fetchone()
            if not digest:raise ValueError('当前签发修订缺失，未重建')
            heads.append({'oid':oid,'day':row[0],'store':row[1],'name':basis.get('name') or row[1],
                          'number':basis['number'],'warehouse':wh,'revision':row[3],'current_revision':row[3],
                          'digest':digest[0],'visibility_token':visible[1] if visible else 0})
            progress('核对原仓单：'+basis['number'])
    return heads
