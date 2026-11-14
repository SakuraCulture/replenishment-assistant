"""Explicit recovery of fixed export revisions; this module never signs orders."""
import json
import uuid
from pathlib import Path
from contextlib import closing
import export_receipts as receipts
from export_files import sha256
from runtime_support import checkpoint


def load(database, task_id):
    if len(task_id) != 32 or any(c not in '0123456789abcdef' for c in task_id):
        raise ValueError('无效导出任务')
    value = json.loads((receipts.directory(database) / (task_id + '.json')).read_text(encoding='utf-8'))
    if value.get('schema') != 1 or value.get('database') != str(Path(database).resolve()):
        raise ValueError('导出任务不属于当前资料目录')
    return value


def original(repo, head):
    import archive_orders
    from warehouse_orders import child_number
    from warehouse_execution import get_order
    order = get_order(repo,head,view='export')
    if not order or any(order[k] != head[k] for k in ('oid', 'digest', 'revision')) or child_number(order['number'], head['warehouse']) != head['number']:
        raise ValueError('原签发修订不存在或身份不一致；不能改用当前版本')
    with closing(repo.work.connect()) as db:
        archive_orders.require_active(db, head['oid'], head['warehouse'])
    return order


def inspect(database, task_id, progress=lambda _: None, cancel=None):
    from history_read import repository
    repo = repository(database)
    receipt = load(database, task_id)
    items = []
    for index, item in enumerate(receipt['files']):
        checkpoint(cancel)
        progress(f'核对导出文件 {index + 1}/{len(receipt["files"])}')
        output = item.get('recovered') or item
        path = Path(output['path'])
        heads = item.get('heads') or ([item['head']] if item.get('head') else [])
        row = dict(index=index, path=str(path), heads=heads, action='blocked', reason='')
        try:
            if not heads:
                raise ValueError('提交时未能留存确切签发修订。请按原日期、门店到订单跟踪核对，不能自动重签或猜修订。')
            # Recheck order availability even when the exported file survives.
            for head in heads:
                original(repo, head)
            same = bool(output.get('sha256') and path.is_file() and sha256(path) == output['sha256'])
            if same:
                row.update(action='record' if receipt['kind'] == 'merchant' and item['state'] == '已生成' else 'keep',
                           reason='原文件及摘要一致；仅补导出记录' if item['state'] == '已生成' else '文件完整保留，无需重复生成')
            else:
                row.update(action='export', reason='原文件不存在、已改变或摘要未留存；在新的恢复目录按原修订补导出，保留原文件')
        except (OSError, ValueError) as exc:
            row['reason'] = str(exc)
        items.append(row)
    return {'receipt': receipt, 'items': items}


def recover(database, review, selected, folder, progress=lambda _: None, cancel=None):
    from history_read import repository
    import normal_orders
    import work_repository as wr
    import normal_delivery
    import archive_batch
    receipt = load(database, review['receipt']['id'])
    if receipt != review['receipt']:
        raise ValueError('导出回执已变化，请重新核对')
    selected = list(selected)
    if not selected or len(set(selected)) != len(selected) or any(type(n) is not int or n < 0 or n >= len(review['items']) for n in selected):
        raise ValueError('请选择明确且不重复的未完成文件')
    fresh = inspect(database, receipt['id'], progress, cancel)
    chosen = [fresh['items'][n] for n in selected]
    if any(r['action'] == 'blocked' for r in chosen):
        raise ValueError('所选项仍有原修订待核，请先处理，未补导出任何文件')
    # Writable existing repository only; no schema initialization or migration.
    repo = normal_orders.Repository.__new__(normal_orders.Repository)
    repo.work = wr.Repository.__new__(wr.Repository)
    repo.work.path = Path(database).resolve()
    root = Path(folder).resolve() / ('恢复导出-' + uuid.uuid4().hex[:12])
    if any(r['action'] == 'export' for r in chosen):
        root.mkdir(parents=True, exist_ok=False)
    completed = []
    for row in chosen:
        checkpoint(cancel)
        item = receipt['files'][row['index']]
        if row['action'] == 'keep':
            continue
        if row['action'] == 'record':
            head = row['heads'][0]
            original(repo, head)
            detail = {'path': item['path'], 'sha256': item['sha256'], 'warehouse': head['warehouse'],
                      'child_number': head['number'], 'export_task': receipt['id']}
            with closing(repo.work.connect()) as db, db:
                db.execute('BEGIN IMMEDIATE')
                from archive_orders import require_active
                require_active(db,head['oid'],head['warehouse'])
                exists = any(json.loads(r[0]).get('export_task') == receipt['id'] and json.loads(r[0]).get('path') == item['path']
                             and json.loads(r[0]).get('sha256') == item['sha256'] for r in db.execute(
                                 "SELECT detail FROM normal_events WHERE oid=? AND revision=? AND action='带号导出'", (head['oid'], head['revision'])))
                if not exists:
                    db.execute('INSERT INTO normal_events(at,oid,revision,action,detail) VALUES(?,?,?,?,?)',
                               (wr.now(), head['oid'], head['revision'], '带号导出', wr.dumps(detail)))
            item.update(state='已完成', stage='完成');item.pop('error', None)
            completed.append(item['path'])
        elif receipt['kind'] == 'merchant':
            head = row['heads'][0];order = original(repo, head)
            # Each selected original file has a unique directory, even for names
            # that collide across stores/dates; never overwrite a user-edited file.
            target = root / f'{row["index"] + 1:03d}';target.mkdir()
            paths = normal_delivery.export_signed(repo, order, target, [head['warehouse']], progress, cancel)
            item['recovered'] = {'path': paths[0], 'sha256': sha256(paths[0])}
            item['state'] = '已恢复';completed.extend(paths)
        else:
            plan = archive_batch.prepare(database, row['heads'], receipt['plan_kind'], receipt['mode'], progress, cancel)
            result = archive_batch.execute(database, plan, root, False, progress, cancel)
            if result['failed'] or result['cancelled'] or len(result['files']) != 1:
                raise RuntimeError('恢复导出未完成，原文件保留；请查看新导出任务逐文件结果')
            output = result['files'][0]
            item['recovered'] = {'path': output['path'], 'sha256': output['sha256']}
            item['state'] = '已恢复';completed.append(output['path'])
        receipts.update(database, receipt)
    receipt['state'] = '已处理勾选项';receipts.update(database, receipt)
    return {'paths': completed, 'warnings': receipt['warnings']}
