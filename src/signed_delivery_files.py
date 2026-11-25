"""Verified merchant original reuse and interruption-safe historical file publication."""
from contextlib import closing, contextmanager
import hashlib
import os
from pathlib import Path
import tempfile

from archive_orders import require_active
from export_files import publish_new, sha256, verify_identity
import export_receipts as receipts
import normal_exports
import order_identity
from runtime_support import checkpoint
from warehouse_orders import child_number
import work_repository as wr


KEYS = ('oid', 'day', 'store', 'warehouse', 'number', 'revision', 'digest', 'child_revision',
        'source_parent_revision', 'snapshot_digest')


def expected_identity(order, warehouse):
    child = {**order, 'number': child_number(order.get('parent_number') or order['number'], warehouse)}
    return [order_identity.entry(child, warehouse, name) for name in normal_exports.SHEETS]


def verify(path, identities, profile):
    from openpyxl import load_workbook
    verify_identity(path, identities)
    with closing(load_workbook(path, read_only=True, keep_links=False)) as book:
        properties = {property.name: property.value for property in book.custom_doc_props}
        expected = {'签发输出格式': profile['format_version'], '模板SHA256': profile['template_sha256'],
                    '本月新品可编辑': '是' if profile['monthly_new_sheet_editable'] else '否',
                    '版本状态': profile['revision_state']}
        if any(properties.get(key) != value for key, value in expected.items()):
            raise ValueError('原签发文件字节与回执一致，但内部输出格式证据冲突')
        if book[order_identity.SHEET].sheet_state != 'veryHidden':
            raise ValueError('原签发文件的系统关联页状态异常')


def candidates(repository, item, profile):
    """Use order-scoped successful events, including receipts outside the UI's recent list."""
    from export_recovery import load
    head = item['head']
    if not head.get('snapshot_digest') or not head.get('child_revision'):
        return []
    path = repository.work.path
    with closing(repository.work.connect()) as db:
        events = [wr.loads(r[0]) for r in db.execute('''
            SELECT detail FROM normal_events WHERE oid=? AND revision=? AND action='带号导出' ORDER BY id DESC
        ''', (head['oid'], head['revision']))]
    hits = []
    processed = set()
    for event in events:
        if event.get('warehouse') != head['warehouse'] or event.get('child_number') != head['number']:
            continue
        receipt_id, original = event.get('export_task'), event.get('path')
        pair = receipt_id, original
        if not receipt_id or not original or not event.get('sha256') or pair in processed:
            continue
        processed.add(pair)
        try:
            result = load(path, receipt_id)
        except (OSError, ValueError):
            continue
        if result['kind'] != 'merchant':
            continue
        for retained in result['files']:
            if (retained.get('state') == '已完成' and retained.get('path') == original
                    and retained.get('sha256') == event['sha256'] and retained.get('output_profile') == profile
                    and all((retained.get('head') or {}).get(k) == head.get(k) for k in KEYS)):
                hits.append({'path': original, 'sha256': event['sha256'], 'receipt': receipt_id})
    hits.sort(key=lambda row: os.path.normcase(row['path']) != os.path.normcase(item['path']))
    return hits


@contextmanager
def protected_file(path):
    """Hold a Windows read handle that disallows writes and replacements."""
    if os.name != 'nt':
        raise OSError('此系统不支持保持原地文件的共享保护')
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    handle = create(str(path), 0x80000000, 1, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(descriptor, 'rb') as stream:
        yield stream


def _copy(source, target, cancel):
    digest = hashlib.sha256()
    with open(source, 'rb') as reader, open(target, 'wb') as writer:
        while block := reader.read(1024 * 1024):
            checkpoint(cancel)
            writer.write(block)
            digest.update(block)
    return digest.hexdigest()


def check_order(db, head):
    require_active(db, head['oid'], head['warehouse'])
    if head.get('snapshot_digest') and head.get('child_revision'):
        import purchases
        purchases.validate_head(db, head)
        row = db.execute('SELECT snapshot_digest FROM warehouse_execution_revisions '
                         'WHERE oid=? AND warehouse=? AND source_revision=?',
                         (head['oid'], head['warehouse'], head['revision'])).fetchone()
        if not row or row[0] != head['snapshot_digest']:
            raise wr.Conflict('原仓单快照摘要已变化')
    else:
        row = db.execute('SELECT digest FROM normal_revisions WHERE oid=? AND revision=?',
                         (head['oid'], head['revision'])).fetchone()
        if not row or row[0] != head['digest']:
            raise wr.Conflict('原签发修订已变化')


def log_once(db, item, task):
    head = item['head']
    detail = {'path': item['path'], 'sha256': item['sha256'], 'warehouse': head['warehouse'],
              'child_number': head['number'], 'export_task': task, 'output_profile': item.get('output_profile')}
    for row in db.execute("SELECT detail FROM normal_events WHERE oid=? AND revision=? AND action='带号导出'",
                          (head['oid'], head['revision'])):
        old = wr.loads(row[0])
        if (old.get('export_task'), old.get('child_number')) == (task, head['number']):
            if (old.get('path'), old.get('sha256')) != (detail['path'], detail['sha256']):
                raise wr.Conflict('该导出任务已有不同文件回执')
            return
    db.execute('INSERT INTO normal_events(at,oid,revision,action,detail) VALUES(?,?,?,?,?)',
               (wr.now(), head['oid'], head['revision'], '带号导出', wr.dumps(detail)))


def publish(repository, receipt, item, path, digest, method, cancel, *, temporary=None, original=None):
    checkpoint(cancel)
    item.update(path=str(path), sha256=digest, stage='等待正式发布', state='待核发布',
                delivery_method=method, reused_from=original,
                publish_intent={'path': str(path), 'sha256': digest})
    filename = repository.work.path
    if not receipts.update(filename, receipt):
        raise OSError('未能保存发布意图，未发布文件')
    checkpoint(cancel)
    with closing(repository.work.connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        check_order(db, item['head'])
        if temporary is not None:
            publish_new(temporary, path)
        log_once(db, item, receipt['id'])
    item.update(state='已完成', stage='完成')
    if not receipts.update(filename, receipt):
        raise OSError('文件已发布，等待补记原回执')
    return str(path)


def deliver(repository, descriptor, load_order, receipt, item, progress, cancel):
    requested = Path(item['path'])
    item['requested_name'] = requested.name
    profile = item['output_profile']
    possible = candidates(repository, item, profile)
    identities = expected_identity(descriptor, item['warehouse']) if possible else None
    for candidate in possible:
        checkpoint(cancel)
        source = Path(candidate['path'])
        if os.path.normcase(str(source)) != os.path.normcase(str(requested)) or not requested.exists():
            continue
        try:
            context = protected_file(source)
            stream = context.__enter__()
        except OSError:
            continue
        try:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != candidate['sha256']:
                continue
            verify(source, identities, profile)
            progress('核验并保留原签发文件')
            return publish(repository, receipt, item, requested, candidate['sha256'], '原地复用',
                           cancel, original=candidate['receipt'])
        finally:
            context.__exit__(None, None, None)
    target = requested
    if target.exists():
        target = requested.parent / ('补导-' + (receipt.get('batch_id') or receipt['id'])[:12]) / requested.name
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=target.parent, prefix='.signed-delivery-', suffix='.xlsx')
    os.close(fd)
    temporary = Path(name)
    try:
        for candidate in possible:
            source = Path(candidate['path'])
            try:
                digest = _copy(source, temporary, cancel)
            except (FileNotFoundError, PermissionError):
                continue
            if digest != candidate['sha256']:
                continue
            verify(temporary, identities, profile)
            progress('核验并复制原签发文件')
            return publish(repository, receipt, item, target, digest, '复制复用', cancel,
                           temporary=temporary, original=candidate['receipt'])
        progress('按原签发修订重建工作簿')
        order = load_order()
        normal_exports.export(order, temporary, cancel=cancel, progress=progress, warehouse=item['warehouse'],
                              monthly_new_sheet_editable=profile['monthly_new_sheet_editable'])
        item['reason'] = '原件缺失、格式不同或摘要未匹配' if possible else '无符合条件的成功回执'
        return publish(repository, receipt, item, target, sha256(temporary), '按原修订重建',
                       cancel, temporary=temporary)
    finally:
        temporary.unlink(missing_ok=True)
