"""Small, local export receipts, separate from immutable business snapshots.

Only exports create this directory. Listing receipts neither initializes a
business database nor loads any order payload. A receipt never authorizes a
signature or overrides a changed/deleted order.
"""
import json
import os
import tempfile
import uuid
import copy,sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path


def directory(database):
    return Path(database).resolve().parent / 'export_receipts'


def stamp():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def identity(order, warehouse):
    from warehouse_orders import child_number
    result = {key: order[key] for key in ('day', 'store', 'oid', 'revision', 'digest')}
    result.update({key:order[key] for key in ('current_revision','child_revision','source_parent_revision','snapshot_digest','execution_epoch','parent_number','visibility_token') if key in order})
    result.update(warehouse=warehouse, number=order['number'] if order.get('warehouse') else child_number(order['number'], warehouse),
                  name=order.get('payload', {}).get('result', {}).get('store_name', order['store']))
    return result


def _save(database, receipt):
    root = directory(database)
    root.mkdir(parents=True, exist_ok=True)
    task_id = receipt['id']
    if len(task_id) != 32 or any(c not in '0123456789abcdef' for c in task_id):
        raise ValueError('导出任务编号无效')
    receipt['updated'] = stamp()
    fd, temporary = tempfile.mkstemp(prefix='.receipt-', suffix='.json', dir=root)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(receipt, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / (task_id + '.json'))
    finally:
        Path(temporary).unlink(missing_ok=True)

def _pruned(db,receipt):
    import deletion_barrier
    if not deletion_barrier.has(db,'deleted_order_tokens') and not deletion_barrier.has(db,'warehouse_execution_heads'):return receipt,False
    files=[];changed=False
    for item in receipt['files']:
        heads=item.get('heads') or ([item['head']] if item.get('head') else [])
        kept=[h for h in heads if deletion_barrier.active(db,h['oid'],h.get('warehouse'))]
        if heads and not kept:changed=True;continue
        if heads!=kept:
            item=copy.deepcopy(item);item['heads']=kept;item.pop('head',None);changed=True
        files.append(item)
    return ({**receipt,'files':files} if changed else receipt),changed

def save(database,receipt):
    # Serialize the final file publication with deletion; keep the caller's
    # in-flight receipt unchanged, while old callbacks cannot restore its heads.
    task_id=receipt['id']
    if not isinstance(task_id,str) or len(task_id)!=32 or any(c not in '0123456789abcdef' for c in task_id):raise ValueError('导出任务编号无效')
    if not Path(database).is_file():return _save(database,receipt)
    with closing(sqlite3.connect(Path(database).resolve().as_uri()+'?mode=rw',uri=True,timeout=10)) as db,db:
        db.execute('BEGIN IMMEDIATE');value,changed=_pruned(db,receipt)
        if changed and not value['files']:(directory(database)/(receipt['id']+'.json')).unlink(missing_ok=True)
        else:_save(database,value)

def prune_deleted(database):
    root=directory(database)
    if not root.exists():return
    for file in root.glob('*.json'):
        if len(file.stem)!=32 or any(c not in '0123456789abcdef' for c in file.stem):continue
        with closing(sqlite3.connect(Path(database).resolve().as_uri()+'?mode=rw',uri=True,timeout=.2)) as db,db:
            db.execute('BEGIN IMMEDIATE')
            try:value=json.loads(file.read_text('utf-8'))
            except FileNotFoundError:continue
            if value.get('schema')!=1 or value.get('database')!=str(Path(database).resolve()):continue
            if value.get('id')!=file.stem:raise ValueError('导出回执文件与任务身份不一致，保留待核')
            value,changed=_pruned(db,value)
            if not changed:continue
            if not value['files']:file.unlink(missing_ok=True)
            else:_save(database,value)


def begin(database, kind, files, **extra):
    receipt = dict(schema=1, id=uuid.uuid4().hex, kind=kind, created=stamp(),
                   database=str(Path(database).resolve()), state='准备中', files=files,
                   warnings=[], **extra)
    # Failure before starting must not leave a new signature without a receipt.
    save(database, receipt)
    return receipt


def update(database, receipt):
    """Record write failure separately from already completed business effects."""
    try:
        save(database, receipt)
        return True
    except (OSError, ValueError, sqlite3.Error) as exc:
        message = '导出回执未能更新：' + str(exc)
        if message not in receipt['warnings']:
            receipt['warnings'].append(message)
        return False


def recent(database, limit=100):
    root = directory(database)
    if not root.exists():
        return []
    paths = sorted(root.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    result = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if data.get('schema') != 1 or data.get('database') != str(Path(database).resolve()):
                continue
            result.append(data)
        except (OSError, ValueError):
            result.append({'id': path.stem, 'state': '回执无法读取，请核对原目录', 'created': '',
                           'files': [], 'warnings': [str(path)]})
    return result


def summary(receipt):
    files = receipt['files']
    complete = [f for f in files if f['state'] in ('已生成', '已完成')]
    remaining = [f for f in files if f not in complete]
    sign = receipt.get('signature', '原归档不变')
    from work_failures import format_failure
    parts = ([format_failure(receipt['failure'])] if receipt.get('failure') else []) + [sign + '。', f'已生成 {len(complete)}/{len(files)} 份：' + ('、'.join(f['path'] for f in complete) or '无'),
             '未生成：' + ('、'.join(f['path'] for f in remaining) or '无')]
    for item in files:
        if item.get('error'):
            parts.append(Path(item['path']).name + '：' + item['error'])
    parts.extend(receipt.get('warnings', []))
    parts.append('可到“订单跟踪 → 导出任务与恢复”查看逐文件结果，按原修订补导出。任务 ' + receipt['id'])
    return '\n'.join(parts)
