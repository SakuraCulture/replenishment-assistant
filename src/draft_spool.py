"""Private signing input shared by the two ends of a local work session."""
import os
import pickle
import shutil
import struct
import tempfile
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

from runtime_support import checkpoint
from work_transfer import resolve_references


OPERATIONS = frozenset(('prepare_normal', 'delivery_prepare', 'delivery_probe', 'delivery_expand'))
_LENGTH = struct.Struct('<Q')
_RESERVE = 16 * 1024**2
_DAMAGED = '签发草稿临时文件不完整，请重新核对'


def temporary_root(database):
    root = Path(database).resolve().parent / '.encoding-cache'
    root.mkdir(exist_ok=True)
    if root.is_symlink() or root.is_junction():
        raise ValueError('签发临时位置不能是目录链接')
    return root


class Writer:
    def __init__(self, stream, folder):
        self.stream = stream
        self.name = Path(stream.name).name
        self.folder = folder
        self.next_check = 0

    def send_bytes(self, data):
        position = self.stream.tell()
        if position >= self.next_check or len(data) > _RESERVE:
            if shutil.disk_usage(self.folder).free < _RESERVE + len(data):
                raise OSError('签发草稿临时盘空间不足，请释放资料盘空间后重试')
            self.next_check = position + _RESERVE
        self.stream.write(_LENGTH.pack(len(data)))
        self.stream.write(data)


@contextmanager
def create(database):
    folder = temporary_root(database)
    # The reader also shares deletion on Windows. The operating system
    # removes this file once both owners close it, including on process exit.
    with tempfile.NamedTemporaryFile(dir=folder, prefix='draft-', suffix='.tmp') as stream:
        yield Writer(stream, folder)


class StagedArray(Sequence):
    def __init__(self, reader):
        self.reader = reader
        self.parts = []
        self.ends = []

    def append_part(self, part, values):
        self.ends.append(len(self) + len(values))
        self.parts.append(part)

    def __len__(self):
        return self.ends[-1] if self.ends else 0

    def __iter__(self):
        for part in self.parts:
            yield from self.reader.values(part)

    def __getitem__(self, key):
        if isinstance(key, slice):
            start, stop, step = key.indices(len(self))
            if step != 1:
                return [self[i] for i in range(start, stop, step)]
            values = []
            for i in range(bisect_right(self.ends, start), len(self.parts)):
                offset = self.ends[i - 1] if i else 0
                if offset >= stop:
                    break
                values.extend(self.reader.values(self.parts[i])[max(0, start-offset):stop-offset])
            return values
        index = key if key >= 0 else len(self) + key
        if not 0 <= index < len(self):
            raise IndexError(key)
        part = bisect_right(self.ends, index)
        offset = self.ends[part - 1] if part else 0
        return self.reader.values(self.parts[part])[index - offset]

    def materialize(self):
        values = []
        for part in self.parts:
            raw = self.reader.values(part)
            if part[2]:
                values.extend(native(v) for v in raw)
            else:
                values.extend(raw)
        return values


class StagedMap(Mapping):
    def __init__(self, reader):
        self.reader = reader
        self.index = {}

    def append_part(self, part, pairs):
        self.index.update((key, part) for key, _ in pairs)

    def __len__(self):
        return len(self.index)

    def __iter__(self):
        return iter(self.index)

    def __getitem__(self, key):
        return self.reader.values(self.index[key])[key]

    def select(self, keys):
        groups = {}
        for key in keys:
            groups.setdefault(self.index[key], []).append(key)
        result = {}
        for part, fields in groups.items():
            values = self.reader.values(part)
            result.update((field, values[field]) for field in fields)
        return result

    def materialize(self):
        items = self.select(self)
        return {key: native(items[key]) if self.index[key][2] else items[key] for key in self}


class Reader:
    def __init__(self, stream, cancel, progress):
        self.stream = stream
        self.cancel = cancel
        self.progress = progress
        self.references = {}
        self.cached_part = None
        self.cached_values = None
        self.size = os.fstat(stream.fileno()).st_size

    def reference(self, ref):
        if ref.number not in self.references:
            if ref.kind == 'list':
                self.references[ref.number] = StagedArray(self)
            elif ref.kind == 'dict':
                self.references[ref.number] = StagedMap(self)
            else:
                raise ValueError(_DAMAGED)
        return self.references[ref.number]

    def resolve(self, value):
        return resolve_references(value, self.reference)

    def index_tree(self):
        root, started = None, False
        count = 0
        while self.stream.tell() < self.size:
            checkpoint(self.cancel)
            prefix = self.stream.read(_LENGTH.size)
            if len(prefix) != _LENGTH.size:
                raise ValueError(_DAMAGED)
            size, = _LENGTH.unpack(prefix)
            offset = self.stream.tell()
            if size > self.size - offset:
                raise ValueError(_DAMAGED)
            message = self.read(offset, size)
            kind = message[0]
            if kind == 'root' and not started:
                root, started = self.resolve(message[1]), True
            elif kind in ('part', 'rawpart') and started:
                _, ref, data = message
                self.reference(ref).append_part((offset, size, kind == 'part', ref.kind), data)
            elif kind == 'end' and started and self.stream.tell() == self.size:
                return root
            else:
                raise ValueError(_DAMAGED)
            count += 1
            if count % 256 == 0:
                self.progress(count)
        raise ValueError(_DAMAGED)

    def read(self, offset, size):
        self.stream.seek(offset)
        raw = self.stream.read(size)
        if len(raw) != size:
            raise ValueError(_DAMAGED)
        return pickle.loads(raw)

    def values(self, part):
        if self.cached_part == part:
            return self.cached_values
        offset, size, deferred, kind = part
        data = self.read(offset, size)[2]
        if deferred:
            data = self.resolve(data)
        if kind == 'dict':
            data = dict(data)
        self.cached_values, self.cached_part = data, part
        return data

    def release(self):
        self.cached_values = None
        self.cached_part = None
        self.references.clear()


@contextmanager
def open_draft(database, name, cancel, progress):
    if not isinstance(name, str) or Path(name).name != name or not name.startswith('draft-') or not name.endswith('.tmp'):
        raise ValueError(_DAMAGED)
    path = temporary_root(database) / name
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_TEMPORARY', 0)
    with open(path, 'rb', opener=lambda file, _: os.open(file, flags)) as stream:
        reader = Reader(stream, cancel, progress)
        try:
            yield reader.index_tree()
        finally:
            reader.release()


def native(value):
    if isinstance(value, (StagedMap, StagedArray)):
        return value.materialize()
    if type(value) is dict:
        return {key: native(v) for key, v in value.items()}
    if type(value) is list:
        return [native(v) for v in value]
    if type(value) is tuple:
        return tuple(native(v) for v in value)
    return value


class StoreResults(Mapping):
    """Give business checks ordinary objects and retain only the last store."""
    def __init__(self, values):
        self.source = values
        self.cached_key = None
        self.cached_value = None

    def __len__(self):
        return len(self.source)

    def __iter__(self):
        return iter(self.source)

    def __contains__(self, key):
        return key in self.source

    def __getitem__(self, key):
        if self.cached_key != key:
            self.cached_key, self.cached_value = None, None
            self.cached_value = native(self.source[key])
            self.cached_key = key
        return self.cached_value


def result_source(value):
    return value.source if isinstance(value, StoreResults) else value
