"""Windows extended paths for migration file I/O, not stored references."""
import os
from pathlib import Path


def io_path(path):
    path = Path(path)
    if os.name != 'nt':
        return path

    name = os.path.abspath(path)
    if name.startswith(('\\\\?\\', '\\\\.\\')):
        return Path(name)
    if name.startswith('\\\\'):
        return Path('\\\\?\\UNC\\' + name[2:])
    return Path('\\\\?\\' + name)
