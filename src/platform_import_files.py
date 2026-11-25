"""Publish the original import bytes with a recoverable, no-overwrite receipt."""
from hashlib import sha256
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from export_files import publish_new, sha256 as file_hash
from runtime_support import checkpoint


def author(operator, reason):
    if (not isinstance(operator, str) or not isinstance(reason, str) or
            not 1 <= len(operator) <= 80 or not 2 <= len(reason) <= 500 or
            operator != operator.strip() or reason != reason.strip() or
            any(ord(c) < 32 for c in operator + reason)):
        raise ValueError('请填写经办人及核对依据')
    return {'operator': operator, 'reason': reason}


def read_original(file, digest):
    source = Path(file)
    if not source.is_file():
        raise ValueError('本次原件未留存')
    if source.stat().st_size > 64 * 1024 * 1024:
        raise ValueError('原件超过可核验大小，请从原任务查看')
    raw = source.read_bytes()
    if sha256(raw).hexdigest() != digest:
        raise ValueError('本次原件摘要不符，请核对原任务')
    return raw


def write_original(raw, digest, target, suffix, cancel=None):
    checkpoint(cancel)
    target = Path(target).resolve()
    if target.suffix.lower() != suffix or sha256(raw).hexdigest() != digest:
        raise ValueError('导出格式或原件摘要不符')
    if not target.is_file():
        with NamedTemporaryFile(prefix='.导入原件-', dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
                stream.close()
                checkpoint(cancel)
                try:
                    publish_new(temporary, target)
                except FileExistsError:
                    pass
            finally:
                stream.close()
                temporary.unlink(missing_ok=True)
    if file_hash(target) != digest:
        raise FileExistsError('目标位置已有不同文件，请另选文件名')
    return {'path': str(target), 'sha256': digest}
