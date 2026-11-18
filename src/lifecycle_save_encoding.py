"""Single traversal of lifecycle facts; exact existing JSON and digest bytes."""
import hashlib
import work_repository as wr


def encode(payload, *, retain=True, write=None):
    """Return stored JSON and the original file-path-independent fingerprint.

    Only the small top-level files value differs between the two streams. All
    other nested values use the same canonical encoder once. An unchanged save
    still validates every value, including full file metadata, but retains no
    unused complete write string. A binary sink can receive the exact UTF-8
    data in sections as they are checked.
    """
    files=[{k:f[k] for k in ('name','sha')} for f in payload['files']]
    if type(payload) is not dict or any(type(k) is not str for k in payload):
        value=dict(payload); value['files']=files
        raw=wr.dumps(payload)
        if retain and write is not None:write(raw.encode('utf-8'))
        return (raw if retain and write is None else None),hashlib.sha256(wr.dumps(value).encode()).hexdigest()
    digest=hashlib.sha256(); digest.update(b'{'); parts=['{'] if retain and write is None else None
    if retain and write is not None:write(b'{')
    for i,key in enumerate(sorted(payload)):
        prefix=(', ' if i else '')+wr.dumps(key)+': '
        text=wr.dumps(payload[key])  # Also validates non-retained file fields.
        prefix_bytes=prefix.encode('utf-8')
        canonical=(wr.dumps(files) if key=='files' else text).encode('utf-8')
        digest.update(prefix_bytes);digest.update(canonical)
        if parts is not None:parts.extend((prefix,text))
        elif retain:
            write(prefix_bytes);write(text.encode('utf-8') if key=='files' else canonical)
    digest.update(b'}')
    if parts is not None:parts.append('}')
    elif retain:write(b'}')
    return ''.join(parts) if parts is not None else None,digest.hexdigest()
