"""Private, delete-on-close signing spool. It never commits business records.

The JSON separators, key order, UTF-8 and zlib settings are the existing v2
format. Only ownership of the *prepared* bytes changes. One spool serves the
whole signing batch; it lives beside the configured work database.
"""
import hashlib
import json
import shutil
import tempfile
import time
import zlib
from collections import OrderedDict
from pathlib import Path
import snapshot_json as codec
from draft_spool import StagedArray, StagedMap

BUFFER = 128 * 1024
CACHE_BYTES = 64 * 1024**2
RESERVE_BYTES = 16 * 1024**2
SMALL_BLOCK = 1024**2
KEY_CACHE_BYTES = 64 * 1024
KEY_CACHE_ENTRIES = 64
_CONTAINERS = (dict, list, tuple, StagedArray, StagedMap)


class _KeyCosts:
    """Bounded, encode-local costs of immutable string-key sequences only.

    No value or container identity is cached. Retained-byte accounting includes
    a conservative allowance for strings, tuple, integer and mapping entries.
    Optional cache allocation failure falls back to the original calculation.
    """
    def __init__(self):
        self.costs = {}
        self.bytes = 0
        self.disabled = False

    def disable(self):
        self.costs.clear(); self.bytes = 0; self.disabled = True

    def cost(self, value):
        count = len(value)
        if self.disabled or type(value) is not dict or not 4 <= count <= 128:
            return 4*count+2+sum(6*len(k)+4 if isinstance(k,str) else 128 for k in value)
        try:
            keys = tuple(value)
            # Do not invoke arbitrary key hashes/equality or cache str subclasses.
            if not all(type(k) is str for k in keys):
                return 4*count+2+sum(6*len(k)+4 if isinstance(k,str) else 128 for k in keys)
            known = self.costs.get(keys)
            if known is not None:return known
            characters = sum(map(len,keys))
            cost = 8*count+2+6*characters
            retained = 256+8*count+4*characters+128*count
            if (len(self.costs) < KEY_CACHE_ENTRIES and self.bytes+retained <= KEY_CACHE_BYTES
                    and all(len(k) <= 64 for k in keys)):
                self.costs[keys] = cost; self.bytes += retained
            return cost
        except MemoryError:
            self.disable()
            return 4*count+2+sum(6*len(k)+4 if isinstance(k,str) else 128 for k in value)


def _small(value, budget, probe=None):
    """Conservative UTF-8/escaping bound, short-circuiting at BUFFER bytes."""
    if isinstance(value,(StagedArray,StagedMap)):return False
    budget[1] -= 1
    if budget[1] < 0: return False
    if isinstance(value,str):budget[0] -= 6*len(value)+4
    elif isinstance(value,dict):
        # A structural lower bound can reject oversized plain containers before
        # walking keys. It never grants the fast encoder permission to run.
        if type(value) is dict and 4*len(value)+2 > budget[0]:return False
        budget[0] -= (probe.cost(value) if probe is not None else
                      4*len(value)+2+sum(6*len(k)+4 if isinstance(k,str) else 128 for k in value))
        for item in value.values():
            if isinstance(item,str):budget[0]-=6*len(item)+4
            elif isinstance(item,_CONTAINERS):
                if not _small(item,budget,probe):return False
            elif isinstance(item,int):budget[0]-=max(8,item.bit_length()//3+4)
            else:budget[0]-=128
            if budget[0]<0:return False
    elif isinstance(value,(list,tuple)):
        if type(value) in (list,tuple) and 2*len(value)+2 > budget[0]:return False
        budget[0] -= 2*len(value)+2
        for item in value:
            if isinstance(item,str):budget[0]-=6*len(item)+4
            elif isinstance(item,_CONTAINERS):
                if not _small(item,budget,probe):return False
            elif isinstance(item,int):budget[0]-=max(8,item.bit_length()//3+4)
            else:budget[0]-=128
            if budget[0]<0:return False
    else:budget[0] -= 128
    return budget[0]>=0


def _parts(value, fast_limit=BUFFER, on_pressure=None, probe=None):
    if fast_limit > 0 and _small(value,[fast_limit,8192],probe):
        # Keep CPython's fast canonical encoder for *proven bounded* containers.
        try:
            text = codec.dumps(value)
        except MemoryError:
            if probe is not None:probe.disable()
            if on_pressure is not None:on_pressure()
        else:
            yield text
            return
        # No bytes from this container have been emitted. Resume its canonical
        # representation in bounded pieces rather than replaying a business step.
        yield from _parts(value,0,on_pressure,probe)
    elif isinstance(value,(dict,StagedMap)):
        yield '{'
        for i,key in enumerate(sorted(value)):
            if i:yield ', '
            if isinstance(key,str):name=key
            elif key is True:name='true'
            elif key is False:name='false'
            elif key is None:name='null'
            elif isinstance(key,(int,float)):name=json.dumps(key,allow_nan=False)
            else:raise TypeError('无效JSON对象键')
            yield codec.dumps(name);yield ': '
            yield from _parts(value[key],fast_limit,on_pressure,probe)
        yield '}'
    elif isinstance(value,(list,tuple,StagedArray)):
        yield '['
        for i,item in enumerate(value):
            if i:yield ', '
            yield from _parts(item,fast_limit,on_pressure,probe)
        yield ']'
    elif isinstance(value,str):
        yield '"'
        for start in range(0,len(value),BUFFER//8):
            yield json.encoder.encode_basestring(value[start:start+BUFFER//8])[1:-1]
        yield '"'
    else:yield codec.dumps(value)


def json_bytes(value, *, fast_limit=BUFFER, on_pressure=None):
    pending = bytearray()
    try:probe = _KeyCosts() if fast_limit > 0 else None
    except MemoryError:probe = None
    for token in _parts(value,fast_limit,on_pressure,probe):
        # Even an unusually long scalar must not create another full UTF-8 copy.
        for start in range(0, len(token), BUFFER // 4):
            part = token[start:start + BUFFER // 4].encode('utf-8')
            if len(pending) + len(part) > BUFFER:
                yield bytes(pending)
                pending.clear()
            pending.extend(part)
    if pending:
        yield bytes(pending)


def digest(value):
    h = hashlib.sha256()
    for raw in json_bytes(value):
        h.update(raw)
    return h.hexdigest()


class Blocks:
    """Small per-manifest index into one batch's shared spool."""
    def __init__(self, store):
        self.store = store
        self.keys = {}

    def __len__(self): return len(self.keys)
    def __iter__(self): return iter(self.keys)

    def items(self):
        # Compatibility for maintenance/tools; signing put() uses streaming I/O.
        for key in self.keys:
            size, _, length, _ = self.store.index[key]
            yield key, (size, b''.join(self.store.read(key)))


class Store:
    def __init__(self, database, progress=lambda _: None, *, cache_bytes=None, resources=None):
        from adaptive_resources import Budget
        self.resources = resources or Budget()
        self.explicit_cache_limit = cache_bytes
        self.root = Path(database).resolve().parent / '.encoding-cache'
        self.root.mkdir(exist_ok=True)
        if self.root.is_symlink() or self.root.is_junction():
            raise ValueError('签发编码临时位置不能是目录链接')
        self.file = tempfile.TemporaryFile(prefix='signing-', suffix='.tmp', dir=self.root)
        self.location = str(getattr(self.file, 'name', self.root))
        self.progress = progress
        self.index = {}
        self.cache = OrderedDict()
        self.cache_limit = self.resources.cache_bytes if cache_bytes is None else min(cache_bytes,self.resources.cache_bytes)
        self.cached = self.cache_peak = self.raw_bytes = self.largest = self.largest_packed = self.reused = 0
        self.closed = False
        self.last_space_check = -RESERVE_BYTES
        self.last_tick = time.monotonic()
        self.encoded_bytes = 0
        self.pressure_fallbacks = 0

    def _refresh(self, force=False):
        changed=self.resources.refresh(force=force)
        self.cache_limit=self.resources.cache_bytes if self.explicit_cache_limit is None else min(self.explicit_cache_limit,self.resources.cache_bytes)
        while self.cache and self.cached>self.cache_limit:
            self.cached-=len(self.cache.popitem(last=False)[1])
        if changed:self.progress('签发资源自适应：'+self.resources.description())

    def _pressure(self):
        self.pressure_fallbacks+=1
        self.cache.clear();self.cached=0
        self.resources.emergency();self._refresh()

    def __enter__(self):
        try:self.progress('签发编码临时位置：' + str(self.root) + '；'+self.resources.description()+'（不含输入数据）')
        except BaseException:
            self.close();raise
        return self

    def __exit__(self, kind, exc, tb):
        if exc is not None:
            try: exc.encoding_stats = self.stats()
            except Exception: pass
        try:self.close()
        except Exception as cleanup:
            if exc is None:raise
            try:exc.encoding_cleanup_error=repr(cleanup)
            except Exception:pass

    def close(self):
        self.cache.clear()
        self.cached = 0
        self.file.close()  # Windows delete-on-close also applies after a crash.
        self.closed = True

    def stats(self):
        return dict(temporary_location=self.location, temporary_bytes=self.file.seek(0, 2),
                    raw_bytes=self.raw_bytes, largest_raw_block=self.largest,
                    largest_compressed_block=self.largest_packed,
                    unique_blocks=len(self.index), reused_blocks=self.reused,
                    cache_bytes=self.cached, cache_peak_bytes=self.cache_peak,
                    cache_limit_bytes=self.cache_limit,
                    resource_budget=self.resources.stats(),pressure_fallbacks=self.pressure_fallbacks,
                    disk_free_bytes=shutil.disk_usage(self.root).free)

    def _write(self, data):
        pos = self.file.tell()
        if pos - self.last_space_check >= RESERVE_BYTES:
            if shutil.disk_usage(self.root).free < RESERVE_BYTES + len(data):
                raise OSError('签发编码临时盘空间不足，尚未提交；请释放当前资料盘空间后重试')
            self.last_space_check = pos
        if data:
            if self.file.write(data) != len(data):
                raise OSError('签发编码临时文件写入不完整，尚未提交')

    def encode(self, value, feed, fragment=False):
        self._refresh()
        start = self.file.seek(0, 2)
        raw_hash = hashlib.sha256(); packed_hash = hashlib.sha256()
        compressor = None; small = bytearray()
        size = 0; tail = b''; first = True
        for raw in json_bytes(value,fast_limit=self.resources.fast_json_bytes,on_pressure=self._pressure):
            raw_hash.update(raw); size += len(raw)
            if compressor is None and len(small)+len(raw)<=SMALL_BLOCK:small.extend(raw)
            else:
                if compressor is None:
                    compressor=zlib.compressobj()
                    packed=compressor.compress(small);small=None
                    self._write(packed);packed_hash.update(packed)
                packed = compressor.compress(raw)
                self._write(packed); packed_hash.update(packed)
            self.encoded_bytes += len(raw)
            if time.monotonic()-self.last_tick>=1:
                self._refresh(force=True)
                self.progress('正在流式编码签发内容：已处理 %.1f MiB；临时文件 %.1f MiB；缓存 %.1f / %.1f MiB' %
                    (self.encoded_bytes/1024**2,self.file.tell()/1024**2,self.cached/1024**2,self.cache_limit/1024**2))
                self.last_tick=time.monotonic()
            if fragment:
                if first: raw = raw[1:]; first = False
                data = tail + raw
                if data: feed(data[:-1]); tail = data[-1:]
            else: feed(raw)
        key = raw_hash.hexdigest()
        if compressor is not None:
            packed = compressor.flush()
            self._write(packed); packed_hash.update(packed)
        elif key not in self.index:
            packed=zlib.compress(small)
            self._write(packed);packed_hash.update(packed)
        length = self.file.tell() - start
        self.largest_packed=max(self.largest_packed,length)
        self.raw_bytes += size; self.largest = max(self.largest, size)
        if key in self.index:
            if self.index[key][0] != size: raise ValueError('编码内容块长度冲突')
            self.file.truncate(start); self.file.seek(start); self.reused += 1
        else:
            self.index[key] = (size, start, length, packed_hash.digest())
        return key, size

    def raw(self,key):
        """Bounded decompression for hashes of already encoded business views."""
        decoder=zlib.decompressobj();h=hashlib.sha256();size=0
        for data in self.read(key):
            while data:
                raw=decoder.decompress(data,BUFFER);data=decoder.unconsumed_tail
                h.update(raw);size+=len(raw)
                if raw:yield raw
        raw=decoder.flush();h.update(raw);size+=len(raw)
        if raw:yield raw
        if not decoder.eof or size!=self.index[key][0] or h.hexdigest()!=key:
            raise ValueError('签发编码临时原文校验失败')

    def read(self, key):
        if self.closed: raise ValueError('签发编码临时文件已关闭')
        self._refresh()
        size, offset, length, expected = self.index[key]
        if key in self.cache:
            data = self.cache.pop(key)
            try:self.cache[key] = data
            except MemoryError:self._pressure()
            yield data
            return
        self.file.seek(offset); remaining = length; h = hashlib.sha256()
        small = bytearray() if length <= BUFFER else None
        while remaining:
            data = self.file.read(min(remaining, BUFFER))
            if not data: raise ValueError('签发编码临时内容块缺失')
            remaining -= len(data); h.update(data)
            if small is not None: small.extend(data)
            yield data
        if h.digest() != expected: raise ValueError('签发编码临时内容块校验失败')
        if small is not None and length <= self.cache_limit:
            while self.cache and self.cached + length > self.cache_limit:
                self.cached -= len(self.cache.popitem(last=False)[1])
            try:self.cache[key] = bytes(small)
            except MemoryError:self._pressure()
            else:
                self.cached += length
                self.cache_peak = max(self.cache_peak, self.cached)

    def put(self, db, blocks, cancel):
        from runtime_support import checkpoint
        for key in blocks:
            checkpoint(cancel)
            size, _, length, _ = self.index[key]
            before = db.total_changes
            db.execute('INSERT OR IGNORE INTO snapshot_chunks VALUES(?,?,zeroblob(?))', (key, size, length))
            inserted = db.total_changes != before
            rowid, actual_size = db.execute('SELECT rowid,size FROM snapshot_chunks WHERE hash=?', (key,)).fetchone()
            if actual_size != size: raise ValueError('快照内容块长度不符：' + key)
            if inserted:
                with db.blobopen('snapshot_chunks', 'data', rowid) as blob:
                    for data in self.read(key):
                        checkpoint(cancel); blob.write(data)
            else:
                # Consume and check our private input even if the DB deduplicates it.
                for data in self.read(key): checkpoint(cancel)
