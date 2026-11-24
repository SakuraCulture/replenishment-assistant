"""Freeze and export a complete subset of the existing order graph."""
from contextlib import ExitStack, closing
from datetime import date
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile

from io_stages import stage as measure
from migration_paths import io_path
import order_merge
import order_migration
from runtime_support import checkpoint
from stockout_common import atomic_json, resource_check


def _selection(graph, order_ids, business_day):
    if business_day:
        if order_ids or date.fromisoformat(business_day).isoformat() != business_day:
            raise ValueError('请按一个业务日或勾选订单确定范围')
        requested = {oid for oid, head in graph.order.items() if head['day'] == business_day}
    else:
        requested = set(order_ids)
    if not requested or not requested <= graph.order.keys():
        raise ValueError('所选范围没有有效签发订单，请刷新后重选')

    included = set(requested)
    while True:
        before = len(included)
        for group in graph.groups:
            if included & group:
                included.update(group)
        if len(included) == before:
            break

    entries = []
    business = {}
    retired = {}
    for oid, warehouse in graph.deleted_warehouses:
        retired.setdefault(oid, set()).add(warehouse)
    for oid in sorted(included, key=lambda key: (graph.order[key]['day'], graph.order[key]['number'], key)):
        checkpoint(graph.cancel)
        head = graph.order[oid]
        warehouses = graph.active_warehouses.get(oid, graph.legacy_warehouses.get(oid, set()))
        reasons = graph.bad.get(oid, ())
        if reasons or oid in graph.deleted or warehouses & retired.get(oid, set()):
            reason = '。'.join(reasons) if reasons else '订单与删除或退役事实冲突'
            raise ValueError(f"{head['number']} 无法完整导出。{reason}")
        entries.append({'oid': oid, 'day': head['day'], 'store': head['store'],
                        'name': graph.revision_names.get((oid, head['current'])) or head['store'],
                        'number': head['number'], 'revision': head['current'], 'warehouses': sorted(warehouses),
                        'requested': oid in requested})
        business[oid] = graph.owner_hash(oid)
    return {'requested': sorted(requested), 'business_day': business_day, 'orders': entries,
            'fingerprint': order_merge.digest({'requested': sorted(requested), 'orders': entries, 'business': business}),
            'business': business}


def preview(root, order_ids, business_day, progress=lambda _: None, cancel=None):
    root = order_migration.validate_root(root)
    progress('核对原仓单及共享分配、采购和作业关联…')
    graph = order_merge.Graph(root, progress, cancel, file_fallback=True)
    try:
        return _selection(graph, order_ids, business_day)
    finally:
        graph.close()


def _copy_rows(graph, stage, rows, progress, cancel):
    from export_progress import RowProgress

    for name in order_merge.DBS:
        subset = [row for row in rows if row.db == name]
        if not subset:
            continue
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(target)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            order_merge._ensure_schema(graph, None, name, db, {row.table for row in subset})
            for index, row in enumerate(subset):
                checkpoint(cancel)
                if index % 32 == 0:
                    progress(RowProgress('保存所选订单关联资料', index, len(subset)))
                values = row.values | order_merge.checked_blobs(graph, row)
                columns = ','.join(order_merge.q(key) for key in values)
                placeholders = ','.join('?' for _ in values)
                db.execute(f'INSERT INTO {order_merge.q(row.table)} ({columns}) VALUES ({placeholders})', tuple(values.values()))


def _copy_originals(graph, stage, rows, included, cancel):
    root = io_path(graph.root)
    target = io_path(stage)
    references = set()
    for row in rows:
        references.update(row.refs)
    def copy_file(relative):
        checkpoint(cancel)
        old, new = root / relative, target / relative
        if not old.resolve().is_relative_to(root) or old.is_symlink() or old.is_junction() or not old.is_file():
            raise ValueError('订单关联原件不存在或路径无效：' + relative)
        digest = order_migration.file_hash(old)
        if not new.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(old, new)
        if order_migration.file_hash(new) != digest or order_migration.file_hash(old) != digest:
            raise ValueError('关联原件在导出期间变化：' + relative)
        return digest

    for relative, (owners, refs, _) in graph.files.items():
        if owners & included:
            references.update(refs)
            if not order_migration.owned(relative):
                raise ValueError('导出恢复记录路径无效')
            copy_file(relative)

    paths, contents = {}, {}
    pending = sorted(ref for ref in references if order_merge._absolute(ref))
    seen = set()
    while pending:
        old = pending.pop()
        if old in seen:
            continue
        seen.add(old)
        relative = graph.map.get(old)
        if relative is None:
            source = io_path(old)
            if not source.is_relative_to(root):
                continue
            relative = source.relative_to(root).as_posix()
            if not order_migration.component_artifact(relative):
                continue
        if not order_migration.owned(relative):
            raise ValueError('订单关联原件映射无效：' + old)
        digest = copy_file(relative)
        declared = {sha for sha, copies in graph.contents.items() if relative in copies}
        if declared and digest not in declared:
            raise ValueError('关联原件与留存的校验值不符：' + relative)
        paths[old] = relative
        contents.setdefault(digest, []).append(relative)
        path = target / relative
        if order_migration.component_artifact(relative) and path.suffix.lower() == '.json' and path.stat().st_size < 32 * 1024**2:
            values = order_merge.strings(json.loads(path.read_text('utf-8-sig')))
            pending.extend(ref for ref in values if order_merge._absolute(ref) and ref not in seen)
    for row in rows:
        if row.db != order_merge.DBS[3]:
            continue
        for ref in row.refs:
            relative = 'aoxiang/component-state/' + ref.replace('\\', '/')
            if ref and order_migration.owned(relative) and (root / relative).is_file():
                copy_file(relative)
    if paths:
        atomic_json(target / '迁入原文件映射.json', {'version': 1, 'paths': paths, 'contents': contents})


@measure('migration.selection.export')
def export(root, destination, confirmed, progress=lambda _: None, cancel=None):
    root = order_migration.validate_root(root)
    destination = Path(destination).resolve()
    if destination.exists() or destination.suffix.lower() != '.zip' or destination.is_relative_to(root):
        raise ValueError('请选择资料目录之外尚不存在的 .zip 文件')
    destination.parent.mkdir(parents=True, exist_ok=True)
    resource_check(destination.parent)
    with tempfile.TemporaryDirectory(prefix='selected-orders-', dir=destination.parent) as temporary:
        stage = Path(temporary) / 'settings'
        stage.mkdir()
        with ExitStack() as locks:
            for name in order_merge.DBS:
                checkpoint(cancel)
                path = root / name
                if path.is_file():
                    db = locks.enter_context(closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=.3)))
                    db.execute('BEGIN IMMEDIATE')
            graph = order_merge.Graph(root, progress, cancel, file_fallback=True)
            locks.callback(graph.close)
            current = _selection(graph, () if confirmed['business_day'] else confirmed['requested'], confirmed['business_day'])
            if current['fingerprint'] != confirmed['fingerprint']:
                raise ValueError('原仓单、签发修订或订单关联已变化，请重新核对导出范围')
            included = set(current['business'])
            rows = [row for row in graph.rows if row.owners & included]
            with measure('migration.selection.copy'):
                _copy_rows(graph, stage, rows, progress, cancel)
                _copy_originals(graph, stage, rows, included, cancel)

        with measure('migration.selection.verify'):
            archived = order_merge.Graph(stage, progress, cancel, file_fallback=True)
            try:
                if archived.order.keys() != included or any(archived.bad.get(oid) or archived.owner_hash(oid) != current['business'][oid] for oid in included):
                    raise ValueError('所选订单的资料或关联不完整，未生成迁移包')
            finally:
                archived.close()
        selection = {'requested': current['requested'], 'included': sorted(included), 'business_day': current['business_day']}
        return order_migration.export(stage, destination, progress, cancel, selection=selection)
