"""Bounded transport for owned work snapshots, without changing their stored format.

Only the two ends of a locally created multiprocessing Pipe use this protocol.
It is not a file importer. Small pickle frames keep Qt's main Python thread from
waiting on one giant pickle while a worker thread transfers a complete snapshot.
"""
from collections import deque
from dataclasses import dataclass
import pickle
from runtime_support import checkpoint

@dataclass(frozen=True)
class Reference:
    number:int
    kind:str


def send_tree(connection,value,cancel=None,progress=lambda count:None,*,consume=False):
    pending=deque();serial=0;frames=0;largest=0
    def skeleton(v):
        nonlocal serial
        if type(v) in (list,dict) and len(v)>256:
            ref=Reference(serial,'list' if type(v) is list else 'dict');serial+=1;pending.append((ref,v));return ref
        if type(v) is dict:return {k:skeleton(x) for k,x in v.items()}
        if type(v) is list:return [skeleton(x) for x in v]
        if type(v) is tuple:return tuple(skeleton(x) for x in v)
        return v
    def frame(value):
        write(pickle.dumps(value,protocol=5))
    def write(raw):
        nonlocal frames,largest
        checkpoint(cancel);largest=max(largest,len(raw));connection.send_bytes(raw);frames+=1
        if frames%64==0:progress(frames)
    def block(ref,values):
        # Ordinary record batches need no Python traversal/copy of every field.
        # Keep frames small for the GIL; split unusually large groups, and only
        # then skeletonize a single record containing a large nested container.
        raw=pickle.dumps(('rawpart',ref,values),protocol=5)
        if len(raw)<=1024*1024:write(raw)
        elif len(values)>1:
            middle=len(values)//2;block(ref,values[:middle]);block(ref,values[middle:])
        else:frame(('part',ref,[(k,skeleton(v)) for k,v in values] if ref.kind=='dict' else [skeleton(v) for v in values]))
    frame(('root',skeleton(value)))
    while pending:
        ref,items=pending.popleft()
        if consume:
            # Only the read process owns this decoded tree. Release transferred
            # children as the receiver builds its copy, instead of retaining both.
            def owned_items():
                if ref.kind=='dict':
                    for key in list(items):yield key,items.pop(key)
                else:
                    for i in range(len(items)):
                        item=items[i];items[i]=None;yield item
            iterator=owned_items()
        else:iterator=iter(items.items() if ref.kind=='dict' else items)
        group=[]
        for value in iterator:
            group.append(value)
            if len(group)==64:block(ref,group);group=[]
        if group:block(ref,group)
    frame(('end',{'frames':frames,'largest_frame':largest}));return {'frames':frames,'largest_frame':largest}


def receive_tree(connection,cancel=None,progress=lambda count:None):
    references={};root=None;seen_root=False;frames=0
    def resolve(v):
        if type(v) is Reference:
            if v.number not in references:references[v.number]=[] if v.kind=='list' else {}
            return references[v.number]
        if type(v) is dict:return {k:resolve(x) for k,x in v.items()}
        if type(v) is list:return [resolve(x) for x in v]
        if type(v) is tuple:return tuple(resolve(x) for x in v)
        return v
    while True:
        checkpoint(cancel)
        while not connection.poll(.05):checkpoint(cancel)
        message=pickle.loads(connection.recv_bytes());frames+=1
        if message[0]=='root':
            if seen_root:raise ValueError('快照传输重复起点')
            root=resolve(message[1]);seen_root=True
        elif message[0]=='part':
            ref,values=message[1:];target=resolve(ref)
            if ref.kind=='list':target.extend(resolve(v) for v in values)
            else:
                for k,v in values:target[k]=resolve(v)
        elif message[0]=='rawpart':
            ref,values=message[1:];target=resolve(ref)
            if ref.kind=='list':target.extend(values)
            else:target.update(values)
        elif message[0]=='end':
            if not seen_root:raise ValueError('快照传输缺少起点')
            return root,message[1]
        else:raise ValueError('未知快照传输数据')
        if frames%64==0:progress(frames)
