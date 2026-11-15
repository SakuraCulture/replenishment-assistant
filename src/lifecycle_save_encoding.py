"""Single traversal of lifecycle facts; exact existing JSON and digest bytes."""
import hashlib
import work_repository as wr


def encode(payload, *, retain=True):
    """Return stored JSON and the original file-path-independent fingerprint.

    Only the small top-level files value differs between the two streams. All
    other nested values use the same canonical encoder once. An unchanged save
    still validates every value, including full file metadata, but retains no
    unused complete write string.
    """
    files=[{k:f[k] for k in ('name','sha')} for f in payload['files']]
    if type(payload) is not dict or any(type(k) is not str for k in payload):
        value=dict(payload); value['files']=files
        raw=wr.dumps(payload)
        return (raw if retain else None),hashlib.sha256(wr.dumps(value).encode()).hexdigest()
    digest=hashlib.sha256(); digest.update(b'{'); parts=['{'] if retain else None
    for i,key in enumerate(sorted(payload)):
        prefix=(', ' if i else '')+wr.dumps(key)+': '
        text=wr.dumps(payload[key])  # Also validates non-retained file fields.
        canonical=wr.dumps(files) if key=='files' else text
        digest.update(prefix.encode('utf-8')); digest.update(canonical.encode('utf-8'))
        if retain:parts.extend((prefix,text))
    digest.update(b'}')
    if retain:parts.append('}')
    return ''.join(parts) if retain else None,digest.hexdigest()
