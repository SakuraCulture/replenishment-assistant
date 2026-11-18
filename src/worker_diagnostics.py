"""Preserve exceptions from headless multiprocessing workers."""
import os
from pathlib import Path
import sys


class WorkerStderr:
    def __init__(self, prefix=None):
        self.prefix = prefix
        self.stream = None

    def write(self, text):
        if not text:
            return 0
        try:
            if self.stream is None:
                prefix = self.prefix
                if prefix is None:
                    from data_location import hint
                    data = hint()
                    prefix = data.parent / ('.' + data.name + '.diagnostics') / 'worker-errors' / 'spawn'
                path = Path(f'{prefix}-{os.getpid()}.log')
                path.parent.mkdir(parents=True, exist_ok=True)
                self.stream = path.open('a', encoding='utf-8', errors='backslashreplace', buffering=1)
            self.stream.write(text)
        except (OSError, ValueError, MemoryError):
            # An exhausted process must still exit with its original error.
            pass
        return len(text)

    def flush(self):
        if self.stream is not None:
            try:
                self.stream.flush()
            except (OSError, ValueError, MemoryError):
                pass


def ensure_stderr(prefix=None):
    if sys.stderr is None or (prefix is not None and isinstance(sys.stderr, WorkerStderr) and sys.stderr.stream is None):
        sys.stderr = WorkerStderr(prefix)
