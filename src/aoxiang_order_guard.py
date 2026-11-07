"""Cheap read-only order existence checks; no snapshot decoding or platform calls."""
import sqlite3
from pathlib import Path
from contextlib import closing


def deleted_orders(path,items):
    path=Path(path).resolve()
    if not path.is_file():raise ValueError('订单数据库不存在，未执行采购；请检查当前数据目录')
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
        db.execute('PRAGMA query_only=ON')
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing={}
        for item in items:
            head=item.get('head') or item.get('basis') or {}
            oid=head.get('oid')
            if not oid:raise ValueError('旧任务缺少订单身份，不能自动判断或重新导入；请核对原任务')
            row=db.execute('SELECT day,store FROM normal_orders WHERE oid=?',(oid,)).fetchone() if 'normal_orders' in tables else None
            if row is None:missing[item['key']]='本机原订单已删除'
            elif 'archive_visibility' in tables:
                gone=db.execute('SELECT deleted FROM archive_visibility WHERE oid=? AND warehouse=?',(oid,head.get('warehouse'))).fetchone()
                if gone and gone[0]:missing[item['key']]='本机原仓单已移入回收站'
        return missing


def require_orders(path,items):
    missing=deleted_orders(path,items)
    if missing:raise ValueError('以下订单已删除，未新建或执行采购任务，请刷新：\n'+'\n'.join(missing))
