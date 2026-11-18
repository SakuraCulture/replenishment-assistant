"""Exact compact snapshot hashes without a second retained analysis tree.

The byte contract is json.dumps(..., ensure_ascii=False, sort_keys=True,
separators=(',', ':'), allow_nan=False, default=str). Facts are serialized a row
at a time. Only their shared per-order references are deferred until attach().
The temporary stream is private to one validation and is deleted on close.
"""
import hashlib
import json
import os
import struct
import tempfile
from runtime_support import checkpoint
from export_progress import RowProgress

OPTIONS=dict(ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False,default=str)
FRAME=struct.Struct('<qQQ')


def canonical(value):return json.dumps(value,**OPTIONS).encode('utf-8')


def update_value(hasher,value,cancel=None):
    # Metadata can include large reference lists: do not join their JSON tokens.
    encoder=json.JSONEncoder(**OPTIONS)
    for token in encoder.iterencode(value):
        checkpoint(cancel)
        for start in range(0,len(token),65536):hasher.update(token[start:start+65536].encode('utf-8'))


def snapshot_digest(schema,spec,tokens,rows,payments,identifiers,cancel=None):
    h=hashlib.sha256();h.update(b'[')
    for index,value in enumerate((schema,spec,tokens)):
        if index:h.update(b',')
        update_value(h,value,cancel)
    h.update(b',[')
    if isinstance(rows,FactSpool):rows.update_hash(h)
    else:
        for index,row in enumerate(rows):
            checkpoint(cancel)
            if index:h.update(b',')
            h.update(canonical(row))
    h.update(b'],');update_value(h,payments,cancel);h.update(b',');update_value(h,identifiers,cancel);h.update(b']')
    return h.hexdigest()


class FactSpool:
    def __init__(self,folder=None,cancel=None,progress=lambda _:None):
        self.stream=tempfile.TemporaryFile(mode='w+b',buffering=128*1024,prefix='.analytics-verify-',dir=folder)
        self.cancel=cancel;self.progress=progress;self.references=[];self.count=0

    def __enter__(self):return self
    def __exit__(self,*_):self.stream.close();self.references.clear()

    def append(self,row):
        checkpoint(self.cancel)
        if 'references' not in row:reference=-1;prefix=canonical(row);suffix=b''
        else:
            refs=row['references']
            # Strong, operation-local references prevent identity reuse. This
            # only deduplicates the per-order object; never proves validity.
            if not self.references or self.references[-1] is not refs:self.references.append(refs)
            reference=len(self.references)-1
            keys=sorted(row);at=keys.index('references')
            left={k:row[k] for k in keys[:at]};right={k:row[k] for k in keys[at+1:]}
            prefix=canonical(left)[:-1]+(b',' if left else b'')+b'"references":'
            suffix=(b',' if right else b'')+canonical(right)[1:]
        self.stream.write(FRAME.pack(reference,len(prefix),len(suffix)));self.stream.write(prefix);self.stream.write(suffix);self.count+=1

    def update_hash(self,hasher):
        self.stream.flush();end=self.stream.tell();self.stream.seek(0);last=None;encoded=None
        for index in range(self.count):
            checkpoint(self.cancel);raw=self.stream.read(FRAME.size)
            if len(raw)!=FRAME.size:raise ValueError('分析复核暂存记录不完整')
            reference,left,right=FRAME.unpack(raw)
            if left+right>end-self.stream.tell() or reference < -1 or reference>=len(self.references):raise ValueError('分析复核暂存记录无效')
            if index:hasher.update(b',')
            prefix=self.stream.read(left);hasher.update(prefix);prefix=None
            if reference!=-1:
                if reference!=last:encoded=canonical(self.references[reference]);last=reference
                hasher.update(encoded)
            suffix=self.stream.read(right);hasher.update(suffix);suffix=None
            if (index+1)%2048==0:self.progress(RowProgress(f'复核分析摘要：{index+1:,} / {self.count:,} 行',index+1,self.count))
        if self.stream.tell()!=end:raise ValueError('分析复核暂存存在未读取内容')

