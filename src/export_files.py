"""Bounded file verification and publication; never publish a partial workbook."""
import hashlib
import os
from pathlib import Path


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def publish_new(source, target):
    """Same-directory, no-clobber publication on the supported Windows host.

    Windows rename refuses an existing destination, including a racing writer.
    Unlike hard links it also works on Windows filesystems without hard links.
    POSIX rename can replace another file, so retain link's no-clobber contract
    there. Never fall back to copying into the visible final filename.
    """
    source, target = Path(source), Path(target)
    if source.resolve().parent != target.resolve().parent:
        raise ValueError('临时文件必须与导出文件位于同一目录')
    if os.name == 'nt':
        os.rename(source, target)
    else:
        os.link(source, target)


def verify_identity(path, expected):
    import openpyxl
    import order_identity
    book = openpyxl.load_workbook(path, read_only=True, keep_links=False)
    try:
        actual = order_identity.read(book)
        keys = ('sheet', 'number', 'store', 'warehouse', 'day', 'revision', 'digest', 'created')
        def values(entries):
            return [tuple(e.get(k) or '' if k == 'warehouse' else e.get(k) for k in keys) for e in entries]
        if values(actual) != values(expected):
            raise ValueError('生成文件的系统关联信息与本次签发不一致，未发布该文件')
    finally:
        book.close()


def explain_error(exc):
    import errno
    code = getattr(exc, 'errno', None)
    if isinstance(exc, PermissionError):
        return '文件被占用或目录无写入权限。关闭对应 Excel/WPS 文件，或选择可写目录后按原修订重试。'
    if code == errno.ENOSPC:
        return '目标磁盘空间不足。腾出空间或选择其他目录后按原修订重试。'
    if isinstance(exc, FileExistsError):
        return '目标文件已经存在。请核对原文件或选择新的导出位置。'
    if isinstance(exc, FileNotFoundError):
        return '目标目录或所需原件不存在。请核对目录连接和原件留存状态。'
    return str(exc)
