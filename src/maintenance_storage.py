"""Bounded snapshot editing and verified attachment-reference cache for deletion.

Business JSON and the existing chunk format are unchanged. Scratch chunks live
on disk until CAS succeeds; no full workday object is materialized for v2 data.
"""
import hashlib,re,sqlite3,zlib
from collections import OrderedDict
import storage_chunks as chunks
import work_repository as wr
import ijson.backends.yajl2_c as streaming_json
from runtime_support import checkpoint
from export_progress import RowProgress

STORE_MAPS=('states','results','store_presets','store_ui','overrides','default_by_store',
            'preissue_drafts','new_product_bases','_calculation_settings')
HEX_RUN=re.compile(rb'[0-9a-f]{64,}')

def hashes(raw,cancel=None):
    """Same lowercase 64-hex boundaries as the legacy scanner, bounded windows."""
    if isinstance(raw,str):raw=raw.encode('utf-8')
    raw.decode('utf-8')  # retain invalid-text rejection; do not retain the string
    found=set();width=262144
    for start in range(0,len(raw),width):
        checkpoint(cancel)
        # One char before and 64+one after preserve runs crossing window edges.
        segment=raw[max(0,start-1):min(len(raw),start+width+65)]
        for match in HEX_RUN.finditer(segment):
            absolute=max(0,start-1)+match.start()
            if start<=absolute<start+width and match.end()-match.start()==64:
                found.add(match.group().decode('ascii'))
    return sorted(found)

def cache_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS maintenance_chunk_refs(hash TEXT PRIMARY KEY, compressed_sha TEXT NOT NULL, refs TEXT NOT NULL, checksum TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS maintenance_legacy_refs(hash TEXT PRIMARY KEY, info TEXT NOT NULL, checksum TEXT NOT NULL)')

class Inflated:
    def __init__(self,blob,cancel,progress):self.blob=blob;self.cancel=cancel;self.progress=progress;self.pos=0;self.z=zlib.decompressobj();self.pending=b'';self.total=0;self.reported=0;self.sha=hashlib.sha256()
    def read(self,n=65536):
        if n==0:return b''
        n=min(n if n>0 else 65536,65536);checkpoint(self.cancel)
        while True:
            if self.pending:data=self.pending
            elif self.pos<len(self.blob):data=self.blob[self.pos:self.pos+65536];self.pos+=len(data)
            else:
                if not self.z.eof:raise ValueError('旧快照压缩数据不完整')
                return b''
            raw=self.z.decompress(data,n);self.pending=self.z.unconsumed_tail;self.total+=len(raw);self.sha.update(raw)
            if self.total-self.reported>=16*1024*1024:self.progress(f'核对旧格式历史资料：已读取 {self.total//1048576:,} MiB');self.reported=self.total
            if raw:return raw

def legacy_info(db,blob,cancel=None,progress=lambda _:None):
    """Validate legacy JSON incrementally; fold references with last-key-wins."""
    key=hashlib.sha256(blob).hexdigest();row=db.execute('SELECT info,checksum FROM maintenance_legacy_refs WHERE hash=?',(key,)).fetchone()
    if row and hashlib.sha256(row[0].encode()).hexdigest()==row[1]:
        info=wr.loads(row[0])
        if isinstance(info,dict) and isinstance(info.get('refs'),list) and all(isinstance(x,str) and re.fullmatch('[0-9a-f]{64}',x) for x in info['refs']) and isinstance(info.get('work_revision'),(str,type(None))):return info
    stack=[];root=set();work_revision=None;count=0
    def attach(refs):
        nonlocal root
        if not stack:root=refs
        elif stack[-1]['kind']=='map':stack[-1]['values'][stack[-1]['key']]=refs
        else:stack[-1]['refs'].update(refs)
    for prefix,event,value in streaming_json.parse(Inflated(blob,cancel,progress)):
        count+=1
        if count%8192==0:checkpoint(cancel)
        if event in ('start_map','start_array'):
            stack.append({'kind':'map','values':{},'key':None,'date':{}} if event=='start_map' else {'kind':'array','refs':set()})
        elif event=='map_key':stack[-1]['key']=value
        elif event in ('end_map','end_array'):
            node=stack.pop()
            if node['kind']=='map':
                if set(node['values'])=={'__workbook_datetime__','date_only'}:wr.decode_object(node['date'])
                refs=set()
                for keyname,child in node['values'].items():
                    refs.update(child)
                    if len(keyname)>=64:refs.update(hashes(wr.dumps(keyname),cancel))
            else:refs=node['refs']
            attach(refs)
        else:
            refs=set()
            if stack and stack[-1]['kind']=='map' and stack[-1]['key'] in ('__workbook_datetime__','date_only'):stack[-1]['date'][stack[-1]['key']]=value
            if event=='string' and len(value)>=64:refs.update(hashes(wr.dumps(value),cancel))
            elif event=='number':
                if not isinstance(value,int):value=float(value)
                encoded=wr.dumps(value)
                if len(encoded)>=64:refs.update(hashes(encoded,cancel))
            if prefix=='work_revision':work_revision=value
            attach(refs)
    checkpoint(cancel);info={'refs':sorted(root),'work_revision':work_revision};raw=wr.dumps(info)
    db.execute('INSERT OR REPLACE INTO maintenance_legacy_refs VALUES(?,?,?)',(hashlib.sha256(blob).hexdigest(),raw,hashlib.sha256(raw.encode()).hexdigest()))
    return info

def references_for(db,h,size,data,cancel=None):
    checkpoint(cancel);compressed=hashlib.sha256(data).hexdigest()
    row=db.execute('SELECT compressed_sha,refs,checksum FROM maintenance_chunk_refs WHERE hash=?',(h,)).fetchone()
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
        self.cache=OrderedDict();self.cache_size=0;self.reads=0
    def raw(self,node):
        checkpoint(self.cancel);h=node[1]
        if h in self.cache:self.cache.move_to_end(h);return self.cache[h]
        row=self.scratch.execute('SELECT size,data FROM snapshot_chunks WHERE hash=?',(h,)).fetchone()
        if row is None:row=self.source.execute('SELECT size,data FROM snapshot_chunks WHERE hash=?',(h,)).fetchone()
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
        return prepared.tree
    def block(self,value):
        raw=wr.dumps(value).encode();h=hashlib.sha256(raw).hexdigest();checkpoint(self.cancel)
        self.scratch.execute('INSERT OR IGNORE INTO snapshot_chunks VALUES(?,?,?)',(h,len(raw),zlib.compress(raw)))
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
    def legacy_tree(self,blob,expected=None):
        """Stream historical JSON to existing chunk nodes; never build a workday."""
        stream=Inflated(blob,self.cancel,self.progress);events=iter(streaming_json.basic_parse(stream));count=0
        def scalar(event,value):
            return float(value) if event=='number' and not isinstance(value,int) else value
        def item(first):
            # Array elements and deep leaves are the same bounded record units
            # used by the v2 codec. Dates retain its object-hook semantics.
            event,value=first
            if event=='start_map':
                obj={}
                while True:
                    event,key=next(events)
                    if event=='end_map':return wr.decode_object(obj)
                    obj[key]=item(next(events))
            if event=='start_array':
                values=[]
                while True:
                    token=next(events)
                    if token[0]=='end_array':return values
                    values.append(item(token))
            return scalar(event,value)
        def node(first,depth=0):
            nonlocal count
            checkpoint(self.cancel);event,value=first
            if event=='start_map' and depth<5:
                fields={}
                while True:
                    event,key=next(events)
                    if event=='end_map':break
                    fields[key]=node(next(events),depth+1)
                tree=['dict',[[key,fields[key]] for key in sorted(fields)]]
                if set(fields)=={'__workbook_datetime__','date_only'}:return self.block(self.value(tree))
                return tree
            if event=='start_array':
                parts=[];batch=[]
                while True:
                    token=next(events)
                    if token[0]=='end_array':break
                    batch.append(item(token))
                    if len(batch)==chunks.BLOCK_ROWS:parts.append(self.block(batch));batch=[];count+=1
                    if count and count%32==0:self.progress(f'逐段整理旧格式作业：已写入 {count:,} 块');checkpoint(self.cancel)
                if batch:parts.append(self.block(batch))
                return ['array',parts]
            return self.block(item(first))
        tree=node(next(events))
        try:next(events)
        except StopIteration:pass
        else:raise ValueError('旧作业快照包含多余数据')
        if expected and stream.sha.hexdigest()!=expected:raise ValueError('作业快照校验失败')
        return tree
    def rewrite(self,blob,codes,strip_legacy,expected=None):
        manifest=chunks.manifest(blob)
        if manifest is None:
            original=self.legacy_tree(blob,expected)
        else:
            if expected and manifest['digest']!=expected:raise ValueError('作业快照与索引不一致')
            original=manifest['tree']
            if self.checksum(original)!=(manifest['digest'],manifest['size']):raise ValueError('完整快照校验失败')
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
        return wr.summary({'states':states,'results':results,'model_version':self.get(tree,('model_version',)),
                           'input_dirty':self.get(tree,('input_dirty',),True)})
