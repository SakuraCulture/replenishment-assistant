"""Bounded oid batches for read-only projections; no cache or storage state."""


def oid_batches(values, size=400):
    ordered=list(dict.fromkeys(values))
    for start in range(0,len(ordered),size):
        batch=ordered[start:start+size]
        yield ','.join('?' for _ in batch),batch
