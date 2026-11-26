"""Immutable, content-addressed JSON blocks in the existing SQLite transaction.

The canonical JSON stream is identical to snapshot_json.dumps. A manifest is
not a mutable cache: its checksum covers its ordered tree and every block hash.
Only version-2 manifests are supported. Garbage collection lives in maintenance.
"""
import hashlib,json,zlib,sqlite3,uuid,os
from itertools import islice
from dataclasses import dataclass
from pathlib import Path
from io import BytesIO
from contextlib import closing
from io_stages import stage
from export_progress import RowProgress
import snapshot_json as codec
from draft_spool import StagedArray, StagedMap, result_source
from scheduled_snapshots import OrderedResults, Results, Rows

_MAPS=(dict,StagedMap,OrderedResults,Results)
_ARRAYS=(list,tuple,StagedArray,Rows)

MAGIC=b'YTCHUNK2\x00'
BLOCK_ROWS=256
UNSUPPORTED='检测到已停用的旧快照格式；请使用 0.46.0 或更早的保留版本打开该资料库。本版本未修改该快照。'

def require_manifest(blob):
    value=manifest(blob)
    if value is None:raise ValueError(UNSUPPORTED)
    return value

def require_current(db,cancel=None):
    """Check only headers, including unselected owners, before any reclamation.

    This is a format gate, not an integrity shortcut: full manifest/block checks
    still run where the payload is read or its shared references are collected.
    """
    from runtime_support import checkpoint
    available={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ('normal_revisions','normal_work_revisions','warehouse_execution_revisions','jobs','scheduled_values'):
        if table not in available:continue
        for header, in db.execute(f'SELECT substr(payload,1,?) FROM {table}',(len(MAGIC),)):
            checkpoint(cancel)
            if header!=MAGIC:raise ValueError(UNSUPPORTED+'（'+table+'）')

def schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS snapshot_chunks(hash TEXT PRIMARY KEY, size INTEGER NOT NULL, data BLOB NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS snapshot_storage_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')

def known(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='snapshot_chunks'").fetchone():return set()
    return {r[0] for r in db.execute('SELECT hash FROM snapshot_chunks')}

def backup_before_write(path,progress=lambda _:None,*,cancel=None):
    """Back up once before creating any new-format record."""
    path=Path(path)
    def complete(src):
        return (src.execute("SELECT 1 FROM sqlite_master WHERE name='snapshot_storage_meta'").fetchone()
                and src.execute("SELECT 1 FROM snapshot_storage_meta WHERE key='initial_backup'").fetchone())
    with closing(sqlite3.connect(path,timeout=.1)) as src,src:
        if complete(src):return
        from sqlite_write import begin
        begin(src,cancel)
        if complete(src):return
        from schema_backup import backup
        with stage('备份首次写入前的完整数据库',progress):
            dest=backup(path,'before-chunks-',progress)
        schema(src)
        src.execute("INSERT INTO snapshot_storage_meta VALUES('initial_backup',?)",(str(dest),))

@dataclass
class Prepared:
    tree: object
    digest: str
    size: int
    chunks: dict
    binding: str|None=None

    def blob(self):
        raw=codec.dumps({'version':2,'tree':self.tree,'digest':self.digest,'size':self.size,'binding':self.binding}).encode()
        return MAGIC+hashlib.sha256(raw).digest()+zlib.compress(raw)

def node_at(tree,path):
    """Locate a named node without interpreting any packed object blocks."""
    for key in path:
        if tree[0] != 'dict':
            return None
        tree = next((child for name, child in tree[1] if name == key), None)
        if tree is None:
            raise KeyError(key)
    return tree


def _feed_ref(store,ref,feed,fragment=False):
    tail=b'';first=True
    for raw in store.raw(ref[1]):
        if fragment:
            if first:raw=raw[1:];first=False
            data=tail+raw
            if data:feed(data[:-1]);tail=data[-1:]
        else:feed(raw)


def prepare(value,existing=(),progress=lambda _:None,binding=None,*,store=None,rows_from=None):
    """Encode once in bounded row blocks; never materialize a whole JSON string."""
    if rows_from is not None and store is None:
        raise ValueError('复用签发行块必须使用本批私有编码文件')
    if store is None:seen=set(existing);blocks={}
    else:
        from encoding_store import Blocks
        blocks=Blocks(store)
    h=hashlib.sha256();size=0;count=0
    def count_blocks(v,depth=0):
        v=result_source(v)
        if isinstance(v,(Results,OrderedResults)):return len(v)
        if isinstance(v,Rows):return sum((count+BLOCK_ROWS-1)//BLOCK_ROWS for _,count in v.parts.values())
        if isinstance(v,_MAPS) and len(v)>BLOCK_ROWS:return (len(v)+BLOCK_ROWS-1)//BLOCK_ROWS
        if isinstance(v,_MAPS) and depth<5:return sum(count_blocks(x,depth+1) for x in v.values())
        return (len(v)+BLOCK_ROWS-1)//BLOCK_ROWS if isinstance(v,_ARRAYS) and len(v)>BLOCK_ROWS else 1
    total=count_blocks(value)
    def feed(raw):
        nonlocal size
        h.update(raw);size+=len(raw)
    def block(v,fragment=False):
        nonlocal count
        if store is None:
            raw=codec.dumps(v).encode();digest=hashlib.sha256(raw).hexdigest();raw_size=len(raw)
            feed(raw[1:-1] if fragment else raw)
            if digest not in seen:
                blocks[digest]=(len(raw),zlib.compress(raw));seen.add(digest)
        else:
            digest,raw_size=store.encode(v,feed,fragment)
            blocks.keys[digest]=None
        count+=1
        if count%32==0:progress(RowProgress(f'编码内容块：{count:,}；复用 {count-len(blocks):,} 块',count,max(total,count)))
        return ['ref',digest,raw_size]
    def copy_rows():
        nonlocal count
        if rows_from[0]=='ref':
            refs=(rows_from,)
        else:
            refs=rows_from[1]
            feed(b'[')
        for index,ref in enumerate(refs):
            if index:feed(b', ')
            _feed_ref(store,ref,feed,rows_from[0]=='array')
            blocks.keys[ref[1]]=None
        if rows_from[0]=='array':feed(b']')
        count+=len(refs)
        progress(RowProgress(f'复用当前门店商品行：{len(refs):,} 块',count,total))
        return rows_from
    def walk(v,depth=0,target=False):
        v=result_source(v)
        if isinstance(v,_MAPS) and len(v)>BLOCK_ROWS:
            feed(b'{');parts=[];keys=sorted(v)
            for start in range(0,len(keys),BLOCK_ROWS):
                if start:feed(b', ')
                group=keys[start:start+BLOCK_ROWS]
                parts.append(block(v.select(group) if isinstance(v,StagedMap) else {k:v[k] for k in group},True))
            feed(b'}');return ['map',parts]
        if isinstance(v,_MAPS) and depth<5:
            feed(b'{');items=[]
            for i,k in enumerate(sorted(v)):
                if not isinstance(k,(str,int,float,bool)) and k is not None:raise TypeError('无效JSON对象键')
                key=k if isinstance(k,str) else json.dumps(k,ensure_ascii=False,allow_nan=False)
                if i:feed(b', ')
                feed(codec.dumps(key).encode()+b': ')
                child = copy_rows() if target and key=='rows' else walk(v[k],depth+1,rows_from is not None and depth==0 and key=='result')
                items.append([key,child])
            feed(b'}');return ['dict',items]
        if isinstance(v,_ARRAYS) and len(v)>BLOCK_ROWS:
            feed(b'[');parts=[]
            if isinstance(v,Rows):groups=v.blocks(BLOCK_ROWS)
            else:
                values=iter(v)
                groups=iter(lambda:list(islice(values,BLOCK_ROWS)),[])
            for group in groups:
                if parts:feed(b', ')
                parts.append(block(group,True))
            feed(b']');return ['array',parts]
        if isinstance(v,Rows):return block(list(v))
        if isinstance(v,(Results,OrderedResults)):return block(dict(v))
        return block(v)
    with stage('按内容块编码及校验完整快照',progress):tree=walk(value)
    progress(RowProgress(f'快照编码完成：{count:,} 块，新增 {len(blocks):,} 块',count,total))
    return Prepared(tree,h.hexdigest(),size,blocks,binding)

def business_digest(prepared,path,focus_revision=None):
    """Hash canonical business fields from prepared bytes, without re-encoding.

    None requests the streaming-encoder fallback for an uncommon manifest
    shape; it never stands for an absent/zero business value.
    """
    from encoding_store import Blocks
    if not isinstance(prepared.chunks,Blocks):return None
    node=prepared.tree
    try:node=node_at(node,path)
    except KeyError:return None
    if node is None:return None
    if node[0]!='dict':return None
    h=hashlib.sha256();store=prepared.chunks.store
    def emit(n,fragment=False):
        kind=n[0]
        if kind=='ref':_feed_ref(store,n,h.update,fragment)
        elif kind=='dict':
            h.update(b'{')
            for i,(key,child) in enumerate(n[1]):
                if i:h.update(b', ')
                h.update(codec.dumps(key).encode()+b': ');emit(child)
            h.update(b'}')
        elif kind in ('array','map'):
            h.update(b'[' if kind=='array' else b'{')
            for i,child in enumerate(n[1]):
                if i:h.update(b', ')
                emit(child,True)
            h.update(b']' if kind=='array' else b'}')
        else:raise ValueError('未知快照节点')
    if focus_revision is not None:
        h.update(b'{"new_focus": '+codec.dumps(focus_revision).encode()+b', "result": ')
    emit(['dict',[(k,v) for k,v in node[1] if k not in ('created','task','version')]])
    if focus_revision is not None:h.update(b'}')
    return h.hexdigest()


def put(db,p,*,cancel=None,checked_rows=()):
    from runtime_support import checkpoint
    schema(db)
    from encoding_store import Blocks
    if isinstance(p.chunks,Blocks):p.chunks.store.put(db,p.chunks,cancel,checked_rows=checked_rows)
    else:
        for h,(size,data) in p.chunks.items():
            checkpoint(cancel)
            db.execute('INSERT OR IGNORE INTO snapshot_chunks VALUES(?,?,?)',(h,size,data))
    # A partial prepare cannot publish references to absent blocks.
    def refs(n):
        if n[0]=='ref':yield n[1],n[2]
        elif n[0]=='dict':
            for _,child in n[1]:yield from refs(child)
        elif n[0] in ('array','map'):
            for child in n[1]:yield from refs(child)
        else:raise ValueError('未知快照节点')
    for h,size in set(refs(p.tree)):
        if (h,size) in checked_rows:continue
        checkpoint(cancel)
        row=db.execute('SELECT size FROM snapshot_chunks WHERE hash=?',(h,)).fetchone()
        if not row or row[0]!=size:raise ValueError('快照内容块缺失或长度不符：'+h)
    return p.blob()

def manifest(blob):
    if not blob.startswith(MAGIC):return None
    check=blob[len(MAGIC):len(MAGIC)+32];raw=zlib.decompress(blob[len(MAGIC)+32:])
    if hashlib.sha256(raw).digest()!=check:raise ValueError('快照清单校验失败')
    value=codec.loads(raw)
    if value.get('version')!=2:raise ValueError('不支持的快照存储版本')
    return value

class Reader:
    def __init__(self,db,progress=lambda _:None):self.db=db;self.progress=progress;self.reads=0;self.total=0
    def raw(self,node):
        row=self.db.execute('SELECT size,data FROM snapshot_chunks WHERE hash=?',(node[1],)).fetchone()
        if not row:raise ValueError('快照内容块缺失：'+node[1])
        raw=zlib.decompress(row[1])
        if len(raw)!=row[0] or len(raw)!=node[2] or hashlib.sha256(raw).hexdigest()!=node[1]:raise ValueError('快照内容块校验失败：'+node[1])
        self.reads+=1
        if self.reads%32==0:self.progress(RowProgress(f'读取并校验内容块：{self.reads:,} 块',self.reads,self.total) if self.total else f'按需读取并校验内容块：{self.reads:,} 块')
        return raw
    def read(self,node,feed=lambda _:None):
        kind=node[0]
        if kind=='ref':
            raw=self.raw(node);feed(raw);return codec.loads(raw)
        if kind=='dict':
            feed(b'{');result={}
            for i,(k,child) in enumerate(node[1]):
                if i:feed(b', ')
                feed(codec.dumps(k).encode()+b': ');result[k]=self.read(child,feed)
            feed(b'}');return codec.decode_object(result)
        if kind=='map':
            feed(b'{');result={}
            for i,child in enumerate(node[1]):
                if i:feed(b', ')
                raw=self.raw(child);feed(raw[1:-1]);result.update(codec.loads(raw))
            feed(b'}');return codec.decode_object(result)
        if kind=='array':
            feed(b'[');result=[]
            for i,child in enumerate(node[1]):
                if i:feed(b', ')
                raw=self.raw(child);feed(raw[1:-1]);result.extend(codec.loads(raw))
            feed(b']');return result
        raise ValueError('未知快照节点')
    def emit(self,node,feed):
        # Full restore parses one JSON document. This
        # retains the decoder's shared dictionary-key strings across all blocks.
        kind=node[0]
        if kind=='ref':feed(self.raw(node));return
        if kind=='dict':
            feed(b'{')
            for i,(key,child) in enumerate(node[1]):
                if i:feed(b', ')
                feed(codec.dumps(key).encode()+b': ');self.emit(child,feed)
            feed(b'}');return
        if kind in ('array','map'):
            feed(b'[' if kind=='array' else b'{')
            for i,child in enumerate(node[1]):
                if i:feed(b', ')
                feed(self.raw(child)[1:-1])
            feed(b']' if kind=='array' else b'}');return
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

def load(blob,db=None,expected=None,paths=None,binding=None,progress=lambda _:None,*,bounded=False):
    m=require_manifest(blob)
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
    def count_refs(node):
        return 1 if node[0]=='ref' else sum(count_refs(v) for _,v in node[1]) if node[0]=='dict' else sum(count_refs(v) for v in node[1])
    reader.total=count_refs(m['tree'])
    if bounded:
        h=hashlib.sha256();size=0
        def feed(raw):
            nonlocal size
            h.update(raw);size+=len(raw)
        value=reader.read(m['tree'],feed)
        if h.hexdigest()!=m['digest'] or size!=m['size']:raise ValueError('完整快照校验失败')
        progress('完整快照逐块读取及校验完成')
        return value
    h=hashlib.sha256();buffer=BytesIO()
    def feed(raw):h.update(raw);buffer.write(raw)
    reader.emit(m['tree'],feed)
    if h.hexdigest()!=m['digest'] or buffer.tell()!=m['size']:raise ValueError('完整快照校验失败')
    progress('完整快照校验完成，解析数据…')
    return codec.loads(buffer.getvalue())


def load_fields(blob,db,paths,*,binding,progress=lambda _:None):
    """Restore selected paths in their original shape, verifying all read blocks."""
    result={}
    for path,value in load(blob,db,paths=paths,binding=binding,progress=progress).items():
        parent=result
        for key in path[:-1]:parent=parent.setdefault(key,{})
        parent[path[-1]]=value
    return result
