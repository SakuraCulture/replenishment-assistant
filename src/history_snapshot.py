"""Checked list projections of signed snapshots, using the caller's read transaction."""
import hashlib
from io import BytesIO

import new_product_focus
import snapshot_json as codec
import storage_chunks as chunks
import warehouse_execution as execution
import work_repository as wr
from normal_orders import RESULT_DIGEST_EXCLUSIONS, revision_source
from runtime_support import checkpoint


RESULT_FIELDS = ('summary', 'store', 'store_name')


class _UnsupportedRows(Exception):
    """Leave unusual, historically readable node layouts to the original reader."""


def _ordered_fields(node):
    if node and node[0] == 'dict':
        fields = dict(node[1])
        if len(fields) == len(node[1]) and all(isinstance(key, str) for key in fields) and list(fields) == sorted(fields):
            return fields
    return {}


def _rows(reader, node, cancel, original=None, canonical=None):
    """Read one verified row block at a time. Managed callers also hash both streams."""
    if node[0] not in ('ref', 'array'):
        raise _UnsupportedRows
    fragments = node[0] == 'array'
    if fragments and original:
        original(b'[')
        canonical(b'[')
    for i, child in enumerate(node[1] if fragments else (node,)):
        checkpoint(cancel)
        raw = reader.raw(child)
        rows = codec.loads(raw)
        if not isinstance(rows, list) or (fragments and (not rows or not raw.startswith(b'[') or not raw.endswith(b']'))):
            raise _UnsupportedRows
        if original:
            if i:
                original(b', ')
                canonical(b', ')
            encoded = codec.dumps(rows).encode('utf-8')
            original(raw[1:-1] if fragments else raw)
            canonical(encoded[1:-1] if fragments else encoded)
        yield from rows
    if fragments and original:
        original(b']')
        canonical(b']')


def _field(reader, node, original):
    # Decode each complete field with the original JSON object hook. This also
    # verifies unlisted fields and preserves typed dates and nested map keys.
    buffer = BytesIO()

    def feed(raw):
        original(raw)
        buffer.write(raw)

    reader.emit(node, feed)
    return codec.loads(buffer.getvalue())


def _verified_result(reader, fields, profile, collect, original, business, cancel):
    metadata = {}
    if profile is not None:
        revision = profile.get('revision') if isinstance(profile, dict) else None
        business(b'{"new_focus": ' + codec.dumps(revision).encode('utf-8') + b', "result": ')
    original(b'{')
    business(b'{')
    first_business = True
    for i, (name, node) in enumerate(fields.items()):
        if i:
            original(b', ')
        key = codec.dumps(name).encode('utf-8') + b': '
        original(key)
        included = name not in RESULT_DIGEST_EXCLUSIONS
        if included:
            if not first_business:
                business(b', ')
            first_business = False
            business(key)
        if name == 'rows':
            rows = _rows(reader, node, cancel, original, business)
            projection = collect(profile, rows)
            # The whole source remains covered if a projection needs only early rows.
            for _ in rows:
                pass
            continue
        field = _field(reader, node, original)
        if included:
            business(codec.dumps(field).encode('utf-8'))
        if name in RESULT_FIELDS:
            metadata[name] = field
    original(b'}')
    business(b'}' if profile is None else b'}}')
    return metadata, projection


def _managed(reader, manifest, binding, collect, cancel):
    root = _ordered_fields(manifest['tree'])
    result = _ordered_fields(root.get('result'))
    if 'rows' not in result:
        raise _UnsupportedRows
    full, business = hashlib.sha256(), hashlib.sha256()
    size = 0
    profile = work_revision = None

    def original(raw):
        nonlocal size
        size += len(raw)
        full.update(raw)

    original(b'{')
    for i, (name, node) in enumerate(root.items()):
        checkpoint(cancel)
        if i:
            original(b', ')
        original(codec.dumps(name).encode('utf-8') + b': ')
        if name == 'result':
            metadata, projection = _verified_result(reader, result, profile, collect, original, business.update, cancel)
        else:
            field = _field(reader, node, original)
            if name == 'new_focus':
                profile = field
            elif name == 'work_revision':
                work_revision = field
    original(b'}')
    if full.hexdigest() != manifest['digest'] or size != manifest['size']:
        raise ValueError('完整快照校验失败')
    if profile is not None:
        new_product_focus.validate(profile)
    if business.hexdigest() != binding:
        raise ValueError('仓单业务快照校验失败')
    return metadata, work_revision, projection


def _optional(reader, tree, path):
    try:
        return reader.select(tree, path)
    except KeyError:
        return None


def _legacy(reader, tree, collect, work_metadata, cancel):
    # Matches the old export view, including its selected-field validation and
    # old parent-only warehouse semantics. There is no new full-parent check.
    result = _ordered_fields(_ordered_fields(tree).get('result'))
    metadata = {}
    for name in RESULT_FIELDS:
        try:
            metadata[name] = reader.select(tree, ('result', name))
        except KeyError:
            continue
    profile = _optional(reader, tree, ('new_focus',))
    work_revision = _optional(reader, tree, ('work_revision',)) if work_metadata else None
    if 'rows' in result:
        try:
            return metadata, work_revision, collect(profile, _rows(reader, result['rows'], cancel))
        except _UnsupportedRows:
            pass
    return metadata, work_revision, collect(profile, reader.select(tree, ('result', 'rows')))


def read(db, head, collect, *, managed, work_metadata=False, cancel=None):
    """Return metadata and a row projection only after its source checks pass.

    Collectors consume rows in order and hold only the list fields they need.
    They are pure because an unsupported layout can be retried by the old reader.
    """
    if managed:
        _, binding, _, blob = execution.active_snapshot_source(db, head)
    else:
        source = revision_source(db, head['day'], head['store'], head['revision'])
        if not source or source['digest'] != head['digest']:
            raise wr.Conflict('原签发依据已变化，请刷新')
        binding, blob = source['digest'], source['payload']
    manifest = chunks.require_manifest(blob)
    if binding is not None and manifest.get('binding') != binding:
        raise ValueError('归档清单与业务指纹不一致')
    reader = chunks.Reader(db, lambda _: checkpoint(cancel))
    if not managed:
        return _legacy(reader, manifest['tree'], collect, work_metadata, cancel)
    try:
        return _managed(reader, manifest, binding, collect, cancel)
    except _UnsupportedRows:
        payload = execution.restore_snapshot(db, blob, binding, lambda _: checkpoint(cancel), exporting=True)
        result = payload['result']
        return result, payload.get('work_revision'), collect(payload.get('new_focus'), result['rows'])
