"""Short per-warehouse OS handle locks. No PID deletion or browser/global locks."""
import os
import time
from contextlib import contextmanager
from pathlib import Path
from runtime_support import checkpoint


@contextmanager
def acquire(folder, warehouse, cancel=None, timeout=5):
    import warehouse_catalog
    if warehouse not in warehouse_catalog.WAREHOUSES:
        raise ValueError('未知仓库')
    path = Path(folder)/'warehouse_catalogs'/('.'+warehouse+'.write-lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if handle.seek(0, 2) == 0:
            handle.write(b'0'); handle.flush()
        deadline = time.monotonic()+timeout
        while True:
            checkpoint(cancel); handle.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise ValueError(warehouse+'资料正在由其他窗口保存，请稍后重试')
                time.sleep(.05)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
