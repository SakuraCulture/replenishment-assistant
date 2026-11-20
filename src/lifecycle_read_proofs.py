"""Session-only proof of a fully checked stored lifecycle document.

The transaction still reads the complete current SQLite row and hashes every
byte. Only identical, already verified content can reuse a compact projection.
Proofs are process-authenticated; edited, serialized-to-another-process, absent,
or stale proofs simply fall back to the original full decoder. Nothing is
persisted to lifecycle payloads, schemas, files or database caches.
"""
import hashlib,hmac,secrets
import snapshot_json as codec
from order_keys import warehouse_revision

_KEY=secrets.token_bytes(32)


def identity(head,stage):
    return [*warehouse_revision(head), stage]


def _mac(value):
    return hmac.new(_KEY,codec.dumps(value).encode('utf-8'),hashlib.sha256).hexdigest()


def make(head,stage,row,document):
    """Called only after the normal full decode/schema/fingerprint check."""
    try:
        version,saved,digest,raw=row; p=document['payload']
        if not isinstance(raw,str) or not isinstance(p.get('rows'),list):return None
        rows=[]
        for r in p['rows']:
            if type(r) is not dict:return None
            rows.append({k:r[k] for k in ('sku','name','base_quantity') if k in r})
        value={'schema':p['schema'],'rows':rows}
        if 'pending' in p:value['pending']=p['pending']
        if 'basis' in p:
            if type(p['basis']) is not dict:return None
            value['basis']={k:p['basis'][k] for k in ('dependencies',) if k in p['basis']}
        signed=[identity(head,stage),version,saved,digest,hashlib.sha256(raw.encode('utf-8')).hexdigest(),codec.dumps(value)]
        return {'value':signed,'mac':_mac(signed)}
    except (KeyError,TypeError,ValueError):
        return None  # An unsupported shape must use the original decoder.


def reuse(head,stage,row,proof):
    if row is None or type(proof) is not dict:return None
    try:
        value=proof['value']; signature=proof['mac']
        if type(value) is not list or len(value)!=6 or not isinstance(signature,str):return None
        if not hmac.compare_digest(signature,_mac(value)):return None
        version,saved,digest,raw=row
        if value[:4]!=[identity(head,stage),version,saved,digest] or not isinstance(raw,str):return None
        if value[4]!=hashlib.sha256(raw.encode('utf-8')).hexdigest():return None
        return {'version':version,'saved':saved,'digest':digest,'payload':codec.loads(value[5])}
    except (KeyError,TypeError,ValueError):
        return None
