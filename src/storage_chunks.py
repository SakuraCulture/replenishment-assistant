"""Immutable, content-addressed JSON blocks in the existing SQLite transaction.

The canonical JSON stream is identical to work_repository.dumps. A manifest is
not a mutable cache: its checksum covers its ordered tree and every block hash.
Legacy zlib JSON remains readable. No deletion/GC of historical blocks is done.
"""
import hashlib,json,zlib,sqlite3,uuid,os
from dataclasses import dataclass
from pathlib import Path
from contextlib import closing
from io_stages import stage
from export_progress import RowProgress

MAGIC=b'YTCHUNK2\x00'
BLOCK_ROWS=256

def codec():
    import work_repository
    return work_repository

def schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS snapshot_chunks(hash TEXT PRIMARY KEY, size INTEGER NOT NULL, data BLOB NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS snapshot_storage_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')

def known(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='snapshot_chunks'").fetchone():return set()
    return {r[0] for r in db.execute('SELECT hash FROM snapshot_chunks')}

def backup_before_write(path,progress=lambda _:None):
    """Online consistent backup; before creating any new-format record.

    Copies are never overwritten or auto-restored. Concurrent initial writers may
    create two backups; both are valid, and transaction CAS still decides writes.
    """
    path=Path(path)
    with closing(sqlite3.connect(path)) as src:
        present=src.execute("SELECT 1 FROM sqlite_master WHERE name='snapshot_storage_meta'").fetchone()
        if present and src.execute("SELECT 1 FROM snapshot_storage_meta WHERE key='initial_backup'").fetchone():return
        dest=path.with_name(path.stem+'.before-chunks-'+uuid.uuid4().hex+'.sqlite3')
        with stage('备份升级前的完整数据库（保留旧格式）',progress),closing(sqlite3.connect(dest)) as target:
            src.backup(target,pages=1024,progress=lambda status,left,total:progress(RowProgress('备份升级前数据库',total-left,total)))
            if target.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('升级前备份完整性检查失败')
        with src:
            schema(src)
            src.execute("INSERT OR IGNORE INTO snapshot_storage_meta VALUES('initial_backup',?)",(str(dest),))

@dataclass
class Prepared:
    tree: object
    digest: str
    size: int
    chunks: dict
    binding: str|None=None

    def blob(self):
        raw=codec().dumps({'version':2,'tree':self.tree,'digest':self.digest,'size':self.size,'binding':self.binding}).encode()
        return MAGIC+hashlib.sha256(raw).digest()+zlib.compress(raw)

def prepare(value,existing=(),progress=lambda _:None,binding=None):
    """Encode once in bounded row blocks; never materialize a whole JSON string."""
    seen=set(existing);blocks={};h=hashlib.sha256();size=0;count=0
    def feed(raw):
        nonlocal size
        h.update(raw);size+=len(raw)
    def block(v,fragment=False):
        nonlocal count
        raw=codec().dumps(v).encode();digest=hashlib.sha256(raw).hexdigest()
        feed(raw[1:-1] if fragment else raw)
        if digest not in seen:
            blocks[digest]=(len(raw),zlib.compress(raw));seen.add(digest)
        count+=1
        if count%32==0:progress(f'编码内容块：已处理 {count:,} 块 / {size/1048576:.1f} MiB；复用 {count-len(blocks):,} 块')
        return ['ref',digest,len(raw)]
    def walk(v,depth=0):
        if isinstance(v,dict) and depth<5:
            feed(b'{');items=[]
            for i,k in enumerate(sorted(v)):
                if not isinstance(k,str):raise ValueError('快照对象键必须为字符串')
                if i:feed(b', ')
                feed(codec().dumps(k).encode()+b': ');items.append([k,walk(v[k],depth+1)])
            feed(b'}');return ['dict',items]
        if isinstance(v,(list,tuple)) and len(v)>BLOCK_ROWS:
            feed(b'[');parts=[]
            for start in range(0,len(v),BLOCK_ROWS):
                if start:feed(b', ')
                parts.append(block(v[start:start+BLOCK_ROWS],True))
            feed(b']');return ['array',parts]
        return block(v)
    with stage('按内容块编码及校验完整快照',progress):tree=walk(value)
    progress(f'快照编码完成：{count:,} 块，新增 {len(blocks):,} 块')
    return Prepared(tree,h.hexdigest(),size,blocks,binding)

def put(db,p):
    schema(db)
    for h,(size,data) in p.chunks.items():
        db.execute('INSERT OR IGNORE INTO snapshot_chunks VALUES(?,?,?)',(h,size,data))
    # A partial prepare cannot publish references to absent blocks.
    def refs(n):
        if n[0]=='ref':yield n[1],n[2]
        elif n[0]=='dict':
            for _,child in n[1]:yield from refs(child)
        elif n[0]=='array':
            for child in n[1]:yield from refs(child)
        else:raise ValueError('未知快照节点')
    for h,size in set(refs(p.tree)):
        row=db.execute('SELECT size FROM snapshot_chunks WHERE hash=?',(h,)).fetchone()
        if not row or row[0]!=size:raise ValueError('快照内容块缺失或长度不符：'+h)
    return p.blob()

def manifest(blob):
    if not blob.startswith(MAGIC):return None
    check=blob[len(MAGIC):len(MAGIC)+32];raw=zlib.decompress(blob[len(MAGIC)+32:])
    if hashlib.sha256(raw).digest()!=check:raise ValueError('快照清单校验失败')
    value=codec().loads(raw)
    if value.get('version')!=2:raise ValueError('不支持的快照存储版本')
    return value

class Reader:
    def __init__(self,db,progress=lambda _:None):self.db=db;self.progress=progress;self.reads=0
    def raw(self,node):
        row=self.db.execute('SELECT size,data FROM snapshot_chunks WHERE hash=?',(node[1],)).fetchone()
        if not row:raise ValueError('快照内容块缺失：'+node[1])
        raw=zlib.decompress(row[1])
        if len(raw)!=row[0] or len(raw)!=node[2] or hashlib.sha256(raw).hexdigest()!=node[1]:raise ValueError('快照内容块校验失败：'+node[1])
        self.reads+=1
        if self.reads%32==0:self.progress(f'读取并校验内容块：{self.reads:,} 块')
        return raw
    def read(self,node,feed=lambda _:None):
        kind=node[0]
        if kind=='ref':
            raw=self.raw(node);feed(raw);return codec().loads(raw)
        if kind=='dict':
            feed(b'{');result={}
            for i,(k,child) in enumerate(node[1]):
                if i:feed(b', ')
                feed(codec().dumps(k).encode()+b': ');result[k]=self.read(child,feed)
            feed(b'}');return result
        if kind=='array':
            feed(b'[');result=[]
            for i,child in enumerate(node[1]):
                if i:feed(b', ')
                raw=self.raw(child);feed(raw[1:-1]);result.extend(codec().loads(raw))
            feed(b']');return result
        raise ValueError('未知快照节点')
    def select(self,node,path):
        if not path:return self.read(node)
        if node[0]=='dict':
            if path[0]=='*':return {k:self.select(v,path[1:]) for k,v in node[1]}
            child=next((v for k,v in node[1] if k==path[0]),None)
            if child is None:raise KeyError(path[0])
            return self.select(child,path[1:])
        value=self.read(node)
        return select_value(value,path)

def select_value(value,path):
    if not path:return value
    if value is None:raise KeyError(path[0])
    if path[0]=='*':return {k:select_value(v,path[1:]) for k,v in value.items()}
    return select_value(value[path[0]],path[1:])

def load(blob,db=None,expected=None,paths=None,binding=None,progress=lambda _:None):
    m=manifest(blob)
    if m is None:
        raw=zlib.decompress(blob)
        if expected and hashlib.sha256(raw).hexdigest()!=expected:raise ValueError('作业快照校验失败')
        value=codec().loads(raw)
        if paths is None:return value
        out={}
        for path in paths:
            try:part=select_value(value,path)
            except (KeyError,TypeError):continue
            out[tuple(path)]=part
        return out
    if expected and m['digest']!=expected:raise ValueError('作业快照清单与索引不一致')
    if binding is not None and m.get('binding')!=binding:raise ValueError('归档清单与业务指纹不一致')
    if db is None:raise ValueError('分块快照需要数据库连接')
    reader=Reader(db,progress)
    if paths is not None:
        values={}
        for path in paths:
            try:values[tuple(path)]=reader.select(m['tree'],path)
            except KeyError:continue
        return values
    h=hashlib.sha256();size=0
    def feed(raw):
        nonlocal size
        h.update(raw);size+=len(raw)
    value=reader.read(m['tree'],feed)
    if h.hexdigest()!=m['digest'] or size!=m['size']:raise ValueError('完整快照校验失败')
    return value
