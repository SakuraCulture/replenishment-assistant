"""Small, side-effect-free contracts shared by the optional stockout module."""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
import hashlib
import json
import math
import os
import tempfile

TZ = timezone(timedelta(hours=8))
VERSION = 'store-stockout-2'
TAG_VERSION = 'virtual-category-1'
WAREHOUSES = ('山东仓', '河北仓')
VIEWS = {'all': '全量', 'retired': '剔除淘汰品', 'assessment': '剔除考核品', 'both': '剔除淘汰品和考核品'}


def now():
    return datetime.now(TZ).isoformat(timespec='seconds')


def today():
    return datetime.now(TZ).date().isoformat()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


def file_hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.stockout-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(encoded(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def read_json(path, limit=16 * 1024 * 1024):
    path = Path(path)
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('配置超过允许大小，请核对文件：' + path.name)
    return json.loads(raw.decode('utf-8-sig'))


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, str) and not value.strip():return None
        result=float(value)
        return result if math.isfinite(result) else None
    except (TypeError,ValueError):return None


def resource_check(folder, extra=0, *, memory_available=None):
    """Conservative preflight at bounded work boundaries, not an RSS guarantee."""
    import shutil
    from runtime_support import Cancelled
    folder=Path(folder)
    while not folder.exists():folder=folder.parent
    if shutil.disk_usage(folder).free < 256*1024*1024+extra:
        raise Cancelled('分析磁盘余量不足，已安全暂停；请释放空间后继续原任务，不会自动删除历史')
    if memory_available is None and os.name=='nt':
        import ctypes
        class MemoryStatus(ctypes.Structure):
            _fields_=[('length',ctypes.c_ulong),('load',ctypes.c_ulong),
                      *[(name,ctypes.c_ulonglong) for name in ('total','available','page_total','page_available','virtual_total','virtual_available','extended')]]
        info=MemoryStatus();info.length=ctypes.sizeof(info)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(info)):memory_available=info.available
    if memory_available is not None and memory_available<128*1024*1024:
        raise Cancelled('系统可用内存不足，已安全暂停分析；关闭其他大任务后继续原任务')


def date_of(observed):
    value = datetime.fromisoformat(observed)
    if value.tzinfo is None:
        raise ValueError('原件观测时间必须包含时区')
    return value.astimezone(TZ).date().isoformat()


@contextmanager
def task_lock(folder):
    """OS-released lock: no stale PID stealing and no competing analysis writers."""
    import msvcrt
    path = Path(folder) / 'task.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if path.stat().st_size == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise ValueError('已有分析任务在运行，请等待安全结束') from exc
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
