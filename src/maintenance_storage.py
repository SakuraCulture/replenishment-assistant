"""Bounded snapshot editing and verified attachment-reference cache for deletion.

Business JSON and the existing chunk format are unchanged. Scratch chunks live
on disk until CAS succeeds; no full workday object is materialized for v2 data.
"""
import codecs,hashlib,re,sqlite3,zlib
from collections import OrderedDict
import storage_chunks as chunks
import work_repository as wr
from runtime_support import checkpoint
from export_progress import RowProgress

STORE_MAPS=('states','results','store_presets','store_ui','overrides','default_by_store',
            'preissue_drafts','new_product_bases','_calculation_settings')
HEX_RUN=re.compile(rb'[0-9a-f]{64,}')

class EvidenceDigest:
    """Same canonical JSON list digest, without retaining every database payload."""
    def __init__(self):self.sha=hashlib.sha256(b'[');self.count=0
    def append(self,value):
        if self.count:self.sha.update(b', ')
        self.sha.update(wr.dumps(value).encode());self.count+=1
    def extend(self,values):
        for value in values:self.append(value)
    def hexdigest(self):
        h=self.sha.copy();h.update(b']');return h.hexdigest()

def hashes(raw,cancel=None):
    """Exact lowercase 64-hex reference boundaries, bounded windows."""
    if isinstance(raw,str):raw=raw.encode('utf-8')
    decoder=codecs.getincrementaldecoder('utf-8')();found=set();width=262144
    for start in range(0,len(raw),width):
        checkpoint(cancel)
        decoder.decode(raw[start:start+width],final=start+width>=len(raw))
        # One char before and 64+one after preserve runs crossing window edges.
        segment=raw[max(0,start-1):min(len(raw),start+width+65)]
        for match in HEX_RUN.finditer(segment):
            absolute=max(0,start-1)+match.start()
            if start<=absolute<start+width and match.end()-match.start()==64:
                found.add(match.group().decode('ascii'))
    return sorted(found)

def contains_text(stream,needles,cancel=None):
    """Exact Unicode substring semantics; validate all UTF-8 even after a match."""
    decoder=codecs.getincrementaldecoder('utf-8')();tail='';found=False
    overlap=max((len(n) for n in needles),default=1)-1
    while True:
        checkpoint(cancel);raw=stream.read(262144);text=decoder.decode(raw,final=not raw)
        if not found:
            combined=tail+text;found=any(n in combined for n in needles)
            tail=combined[-overlap:] if overlap else ''
        if not raw:return found

def publish_cache(db,scratch):
    """Do not rewrite identical historical reference caches on every deletion."""
    cache_schema(db)
    for table,columns in (('maintenance_chunk_refs',('compressed_sha','refs','checksum')),):
        values=','.join('?' for _ in range(len(columns)+1))
        update=','.join(f'{c}=excluded.{c}' for c in columns)
        different=' OR '.join(f'{table}.{c} IS NOT excluded.{c}' for c in columns)
        db.executemany(f'INSERT INTO {table} VALUES({values}) ON CONFLICT(hash) DO UPDATE SET {update} WHERE {different}',scratch.execute(f'SELECT * FROM {table}'))

def cache_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS maintenance_chunk_refs(hash TEXT PRIMARY KEY, compressed_sha TEXT NOT NULL, refs TEXT NOT NULL, checksum TEXT NOT NULL)')



def references_for(db,h,size,data,cancel=None,*,cache_source=None):
    checkpoint(cancel);compressed=hashlib.sha256(data).hexdigest()
    # During preparation the source cache is read-only. Only a missing or
    # invalid receipt is staged in db, and publication stays inside deletion.
    lookup=cache_source if cache_source is not None else db
    row=lookup.execute('SELECT compressed_sha,refs,checksum FROM maintenance_chunk_refs WHERE hash=?',(h,)).fetchone()
    if row and row[0]==compressed and hashlib.sha256(row[1].encode()).hexdigest()==row[2]:
        refs=wr.loads(row[1])
        if isinstance(refs,list) and all(isinstance(x,str) and re.fullmatch('[0-9a-f]{64}',x) for x in refs):return refs
    raw=zlib.decompress(data);checkpoint(cancel)
    if len(raw)!=size or hashlib.sha256(raw).hexdigest()!=h:raise ValueError('快照内容块校验失败：'+h)
    refs=hashes(raw,cancel);text=wr.dumps(refs)
    db.execute('INSERT OR REPLACE INTO maintenance_chunk_refs VALUES(?,?,?,?)',(h,compressed,text,hashlib.sha256(text.encode()).hexdigest()))
    return refs

class Editor:
    def __init__(self,source,scratch,progress=lambda _:None,cancel=None):
        self.source=source;self.scratch=scratch;self.progress=progress;self.cancel=cancel
        chunks.schema(scratch);cache_schema(scratch)
        self.cache=OrderedDict();self.cache_size=0;self.reads=0;self.scratch_hashes=chunks.known(scratch)
    def raw(self,node):
        checkpoint(self.cancel);h=node[1]
        if h in self.cache:self.cache.move_to_end(h);return self.cache[h]
        first,second=(self.scratch,self.source) if h in self.scratch_hashes else (self.source,self.scratch)
        row=first.execute('SELECT size,data FROM snapshot_chunks WHERE hash=?',(h,)).fetchone()
        if row is None:row=second.execute('SELECT size,data FROM snapshot_chunks WHERE hash=?',(h,)).fetchone()
        if row is None:raise ValueError('快照内容块缺失：'+h)
        raw=zlib.decompress(row[1]);checkpoint(self.cancel)
        if len(raw)!=row[0] or len(raw)!=node[2] or hashlib.sha256(raw).hexdigest()!=h:raise ValueError('快照内容块校验失败：'+h)
        self.reads+=1
        if self.reads%32==0:self.progress(f'逐块核对作业：已读取 {self.reads:,} 块')
        if len(raw)<=8*1024*1024:
            self.cache[h]=raw;self.cache_size+=len(raw)
            while self.cache_size>8*1024*1024:
                _,old=self.cache.popitem(last=False);self.cache_size-=len(old)
        return raw
    def emit(self,node,feed):
        checkpoint(self.cancel);kind=node[0]
        if kind=='ref':feed(self.raw(node));return
        feed(b'{' if kind in ('dict','map') else b'[')
        for i,item in enumerate(node[1]):
            if i:feed(b', ')
            if kind=='dict':
                key,child=item;feed(wr.dumps(key).encode()+b': ');self.emit(child,feed)
            elif kind in ('array','map'):feed(self.raw(item)[1:-1])
            else:raise ValueError('未知快照节点')
        feed(b'}' if kind in ('dict','map') else b']')
    def checksum(self,node):
        sha=hashlib.sha256();size=0
        def feed(raw):
            nonlocal size
            sha.update(raw);size+=len(raw)
        self.emit(node,feed);return sha.hexdigest(),size
    def value(self,node):
        parts=[];self.emit(node,parts.append);return wr.loads(b''.join(parts))
    def encode(self,value):
        def report(s):checkpoint(self.cancel);self.progress(s)
        prepared=chunks.prepare(value,progress=report)
        for h,(size,raw) in prepared.chunks.items():
            checkpoint(self.cancel);self.scratch.execute('INSERT OR IGNORE INTO snapshot_chunks VALUES(?,?,?)',(h,size,raw))
            self.scratch_hashes.add(h)
        return prepared.tree
    def block(self,value):
        raw=wr.dumps(value).encode();h=hashlib.sha256(raw).hexdigest();checkpoint(self.cancel)
        if h not in self.scratch_hashes:
            self.scratch.execute('INSERT INTO snapshot_chunks VALUES(?,?,?)',(h,len(raw),zlib.compress(raw)))
            self.scratch_hashes.add(h)
        return ['ref',h,len(raw)]
    def get(self,node,path,default=None):
        if not path:return self.value(node)
        if node[0]=='dict':
            child=next((v for k,v in node[1] if k==path[0]),None)
            return default if child is None else self.get(child,path[1:],default)
        value=self.value(node)
        for key in path:
            if not isinstance(value,dict) or key not in value:return default
            value=value[key]
        return value
    def update(self,node,path,fn,create=False,default=None):
        checkpoint(self.cancel)
        if not path:return fn(node)
        if node[0]=='dict':
            items=[];found=False
            for key,child in node[1]:
                if key==path[0]:child=self.update(child,path[1:],fn,create,default);found=True
                items.append([key,child])
            if create and not found:
                if len(path)!=1:raise ValueError('不能隐式创建快照父字段')
                items.append([path[0],fn(self.encode(default))]);items.sort(key=lambda p:p[0])
            return ['dict',items]
        value=self.value(node);parent=value
        for key in path[:-1]:
            if not isinstance(parent,dict) or key not in parent:return node
            parent=parent[key]
        if isinstance(parent,dict) and (path[-1] in parent or create):
            parent[path[-1]]=self.value(fn(self.encode(parent.get(path[-1],default))))
            return self.encode(value)
        return node
    def drop_keys(self,node,codes):
        if node[0]=='dict':return ['dict',[[k,v] for k,v in node[1] if k not in codes]]
        if node[0]=='map':
            parts=[]
            for part in node[1]:
                value=wr.loads(self.raw(part));new={k:v for k,v in value.items() if k not in codes}
                if new:parts.append(part if len(new)==len(value) else self.block(new))
            return ['map',parts]
        value=self.value(node)
        return self.encode({k:v for k,v in value.items() if k not in codes}) if isinstance(value,dict) else node
    def filter(self,node,keep):
        if node[0]=='array':
            parts=[]
            for part in node[1]:
                value=wr.loads(self.raw(part));new=[v for v in value if keep(v)]
                if new:parts.append(part if len(new)==len(value) else self.block(new))
            return ['array',parts]
        value=self.value(node);return self.encode([v for v in value if keep(v)])
    def rewrite(self,blob,codes,expected=None):
        manifest=chunks.require_manifest(blob);tree=manifest['tree']
        # The native writer uses ordered dictionaries for these containers. A
        # wide/current-format map or a scalar container keeps the existing path.
        def ordered(node):
            if node[0]!='dict':return False
            keys=[k for k,_ in node[1]]
            return all(isinstance(k,str) for k in keys) and keys==sorted(set(keys))
        if not ordered(tree):return self._rewrite_serial(blob,codes,expected)
        top=dict(tree[1]);loaded=top.get('loaded');intake=None;warehouses=None
        if loaded is not None:
            if not ordered(loaded):return self._rewrite_serial(blob,codes,expected)
            fields=dict(loaded[1]);intake=fields.get('intake');warehouses=fields.get('warehouse_rows')
            if any(n is not None and not ordered(n) for n in (intake,warehouses)):
                return self._rewrite_serial(blob,codes,expected)
        if expected and manifest['digest']!=expected:raise ValueError('作业快照与索引不一致')
        import engine
        config=self.get(tree,('loaded','config')) or engine.config();field=config['sales_fields']['store_code']
        def affected(e):
            if isinstance(e,dict):return e.get('store') in codes or any(affected(v) for v in e.values())
            return isinstance(e,list) and any(affected(v) for v in e)
        drops={(key,) for key in STORE_MAPS}|{('loaded','stores'),('loaded','blocked'),('loaded','intake','stores')}
        filters={('loaded','normalized'):lambda r:r[0].get('B') not in codes,
                 ('loaded','intake','conflicts'):lambda r:r['store'] not in codes,
                 ('loaded','intake','selected'):lambda r:engine.adapter.identifier(r.get('values',{}).get(field))[0] not in codes,
                 ('export_log',):lambda e:not affected(e)}
        if warehouses:
            filters.update({('loaded','warehouse_rows',w):filters[('loaded','normalized')] for w,_ in warehouses[1]})
        defaults={():{'export_log':[],'pending_copy':None}}
        if loaded and loaded[1]:defaults[('loaded',)]={'normalized':[]}
        if intake and intake[1]:defaults[('loaded','intake')]={'stores':{},'conflicts':[],'selected':[]}
        containers={(),('loaded',),('loaded','intake'),('loaded','warehouse_rows')}
        old_sha=hashlib.sha256();new_sha=hashlib.sha256();sizes=[0,0]
        def old(raw):old_sha.update(raw);sizes[0]+=len(raw)
        def new(raw):new_sha.update(raw);sizes[1]+=len(raw)
        def both(raw):old(raw);new(raw)
        def key_bytes(key):return wr.dumps(key).encode()+b': '
        def encoded(value):
            node=self.encode(value);self.emit(node,new);return node
        def filtered(node,keep,is_map=False):
            # Each block supplies the original digest, parsed selection and
            # surviving digest before it leaves the bounded raw-byte cache.
            kind='map' if is_map else 'array'
            if node[0]!=kind:
                raw=[];self.emit(node,raw.append);data=b''.join(raw);old(data)
                value=wr.loads(data)
                if is_map and not isinstance(value,dict):new(data);return node
                return encoded({k:v for k,v in value.items() if k not in codes} if is_map else [v for v in value if keep(v)])
            both(b'{' if is_map else b'[');parts=[]
            for i,part in enumerate(node[1]):
                raw=self.raw(part)
                if i:old(b', ')
                old(raw[1:-1]);value=wr.loads(raw)
                kept={k:v for k,v in value.items() if k not in codes} if is_map else [v for v in value if keep(v)]
                if kept:
                    if parts:new(b', ')
                    if len(kept)==len(value):parts.append(part);new(raw[1:-1])
                    else:parts.append(self.block(kept));new(wr.dumps(kept).encode()[1:-1])
            both(b'}' if is_map else b']');return [kind,parts]
        def walk(node,path):
            checkpoint(self.cancel)
            if path==('pending_copy',):self.emit(node,old);return encoded(None)
            if path in filters:return filtered(node,filters[path])
            if path in drops:
                if node[0]!='dict':return filtered(node,None,True)
                both(b'{');items=[]
                for i,(key,child) in enumerate(node[1]):
                    if i:old(b', ')
                    old(key_bytes(key))
                    if key in codes:self.emit(child,old)
                    else:
                        if items:new(b', ')
                        new(key_bytes(key));self.emit(child,both);items.append([key,child])
                both(b'}');return ['dict',items]
            if path not in containers:self.emit(node,both);return node
            fields=dict(node[1]);extra=defaults.get(path,{})
            both(b'{');items=[];old_count=0
            for key in sorted(fields.keys()|extra.keys()):
                if items:new(b', ')
                new(key_bytes(key))
                if key in fields:
                    if old_count:old(b', ')
                    old(key_bytes(key));old_count+=1
                    child=walk(fields[key],path+(key,))
                else:child=encoded(extra[key])
                items.append([key,child])
            both(b'}');return ['dict',items]
        rewritten=walk(tree,())
        # Even discarded branches feed the full original digest. A mismatch
        # rejects the prepared result before any business transaction begins.
        if (old_sha.hexdigest(),sizes[0])!=(manifest['digest'],manifest['size']):raise ValueError('完整快照校验失败')
        self.original_digest=manifest['digest']
        return chunks.Prepared(rewritten,new_sha.hexdigest(),sizes[1],{})
    def _rewrite_serial(self,blob,codes,expected=None):
        manifest=chunks.require_manifest(blob)
        if expected and manifest['digest']!=expected:raise ValueError('作业快照与索引不一致')
        original=manifest['tree']
        if self.checksum(original)!=(manifest['digest'],manifest['size']):raise ValueError('完整快照校验失败')
        self.original_digest=manifest['digest']
        tree=original
        for key in STORE_MAPS:tree=self.update(tree,(key,),lambda n:self.drop_keys(n,codes))
        for path in (('loaded','stores'),('loaded','blocked')):
            tree=self.update(tree,path,lambda n:self.drop_keys(n,codes))
        row_keep=lambda r:r[0].get('B') not in codes
        loaded_node=next((v for k,v in tree[1] if k=='loaded'),None) if tree[0]=='dict' else None
        intake_node=next((v for k,v in loaded_node[1] if k=='intake'),None) if loaded_node and loaded_node[0]=='dict' else None
        create_intake=bool(intake_node and intake_node[0]=='dict' and intake_node[1])
        tree=self.update(tree,('loaded','intake','stores'),lambda n:self.drop_keys(n,codes),create=create_intake,default={})
        tree=self.update(tree,('loaded','normalized'),lambda n:self.filter(n,row_keep),create=bool(loaded_node and loaded_node[0]=='dict' and loaded_node[1]),default=[])
        # Warehouse container is a small dictionary, lists remain block-addressed.
        def warehouses(node):
            if node[0]=='dict':return ['dict',[[w,self.filter(rows,row_keep)] for w,rows in node[1]]]
            value=self.value(node);return self.encode({w:[r for r in rows if row_keep(r)] for w,rows in value.items()})
        tree=self.update(tree,('loaded','warehouse_rows'),warehouses)
        tree=self.update(tree,('loaded','intake','conflicts'),lambda n:self.filter(n,lambda r:r['store'] not in codes),create=create_intake,default=[])
        import engine
        config=self.get(tree,('loaded','config')) or engine.config();field=config['sales_fields']['store_code']
        tree=self.update(tree,('loaded','intake','selected'),lambda n:self.filter(n,lambda r:engine.adapter.identifier(r.get('values',{}).get(field))[0] not in codes),create=create_intake,default=[])
        def affected(e):
            if isinstance(e,dict):return e.get('store') in codes or any(affected(v) for v in e.values())
            return isinstance(e,list) and any(affected(v) for v in e)
        tree=self.update(tree,('export_log',),lambda n:self.filter(n,lambda e:not affected(e)),create=True,default=[])
        tree=self.update(tree,('pending_copy',),lambda n:self.block(None),create=True)
        sha,size=self.checksum(tree)
        return chunks.Prepared(tree,sha,size,{})
    def value_kind(self,node):
        return 'null' if node[0]=='ref' and self.raw(node)==b'null' else node[0]
    def job_summary(self,tree):
        states=self.get(tree,('states',),{});results={}
        for code in states:
            value=self.get(tree,('results',code,'summary'))
            if value is not None:results[code]={'summary':value}
        missing=object();model_version=self.get(tree,('model_version',),missing)
        if model_version is missing:raise KeyError('model_version')
        return wr.summary({'states':states,'results':results,'model_version':model_version,
                           'input_dirty':self.get(tree,('input_dirty',),True)})
