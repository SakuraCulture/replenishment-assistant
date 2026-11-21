"""Evidence for borrowing only a store's rows from this signing call's draft."""
from dataclasses import dataclass
from datetime import date, datetime
from math import copysign
import json
import zlib

from app_defaults import WAREHOUSES
from archive_orders import warehouse as row_warehouse
from runtime_support import checkpoint
import snapshot_json as codec
import storage_chunks as chunks
from work_repository import Conflict


# Larger valid blocks keep the ordinary streaming encoder. The proof should not
# make an unusually large scalar require a second large decoded copy.
MAX_COMPARE_BYTES = 4 * 1024**2


def _same_value(raw, value):
    """Compare canonical JSON values without serializing the source rows again."""
    kind = type(value)
    if kind is dict:
        if type(raw) is not dict or len(raw) != len(value):
            return False
        if raw.keys() == value.keys():
            return all(_same_value(raw[key], item) for key, item in value.items())
        # Canonical JSON permits homogeneous non-string keys as well.
        if any(type(key) is not str for key in value):
            return _same_value(raw, {key if isinstance(key,str) else json.dumps(key): item for key, item in value.items()})
        return False
    if kind is type(raw) and kind is not list:
        return raw == value and (kind is not float or value != 0 or copysign(1,raw) == copysign(1,value))
    if kind in (list, tuple):
        return type(raw) is list and len(raw) == len(value) and all(_same_value(a,b) for a,b in zip(raw,value))
    if isinstance(value,(date,datetime)):
        return _same_value(raw,codec.encode_default(value))
    return False


def _row_node(tree, path):
    node = chunks.node_at(tree,path)
    if node is None:return None
    if node[0]=='ref':return tuple(node)
    if node[0]=='array':return ('array',tuple(map(tuple,node[1])))
    raise ValueError('商品行块不是有序数组')


def _refs(node):
    return (node,) if node[0]=='ref' else node[1]


def _identity(head):
    return tuple(head[key] for key in ('day','store','oid','number','revision'))


def location(head, warehouse):
    return f"门店 {head['store']} · {warehouse} · 签发修订 {head['revision']}"


def source_digest(pack, head, selected, focus_revision):
    try:
        return chunks.business_digest(pack,('results',head['store']),focus_revision)
    except (KeyError, ValueError, zlib.error) as exc:
        raise Conflict(f'{location(head,"、".join(selected))}：{exc}') from exc


def _field(pack, path):
    node = chunks.node_at(pack.tree,path)
    if node is None or node[0] != 'ref':
        raise ValueError('快照身份字段缺失')
    return json.loads(b''.join(pack.chunks.store.raw(node[1])))


@dataclass(frozen=True)
class Rows:
    spool: object
    node: tuple
    draft_revision: str
    draft_digest: str
    parent: tuple
    warehouse: str
    location: str

    def check_projection(self, source, projected, warehouse):
        if warehouse != self.warehouse or len(source) != len(projected) or any(a is not b for a,b in zip(source,projected)):
            raise Conflict(f'{self.location}：分仓后的商品行、行数或顺序与来源不符')

    def prepare(self, record, progress, binding=None):
        try:
            if record['work_revision'] != self.draft_revision or record['result']['store'] != self.parent[1]:
                raise ValueError('目标草稿版本或门店不符')
            return chunks.prepare(record,progress=progress,binding=binding,store=self.spool,rows_from=self.node)
        except (KeyError, ValueError, zlib.error) as exc:
            raise Conflict(f'{self.location}：{exc}') from exc

    def check(self, work_pack, revision, head, targets):
        if (revision != self.draft_revision or work_pack.digest != self.draft_digest
                or _identity(head) != self.parent or work_pack.chunks.store is not self.spool
                or _row_node(work_pack.tree,('results',head['store'],'rows')) != self.node):
            raise ValueError('来源草稿、门店或签发修订已变化')
        for target, binding in targets:
            if (target is None or target.chunks.store is not self.spool or target.binding != binding
                    or _field(target,('work_revision',)) != revision or _field(target,('result','store')) != head['store']
                    or _row_node(target.tree,('result','rows')) != self.node):
                raise ValueError('待签发清单的商品行与来源不符')


def find(work_pack, spool, revision, head, selected, rows, cancel):
    """Check the complete result, then prove the exact bytes at its draft path."""
    if len(selected) != 1 or selected[0] not in WAREHOUSES or not rows:
        return None
    warehouse = selected[0]
    if any(row_warehouse(row) != warehouse for row in rows):
        return None
    label = location(head,warehouse)
    try:
        if work_pack.chunks.store is not spool:
            raise ValueError('商品行来自其他签发任务')
        if work_pack.tree[0] != 'dict':
            return None  # The draft itself is packed as a map.
        if _field(work_pack,('day',)) != head['day']:
            raise ValueError('来源草稿业务日期不符')
        node = _row_node(work_pack.tree,('results',head['store'],'rows'))
        if node is None:
            return None  # Packed dictionaries are kept on the original path.
        refs = _refs(node)
        if len(refs) != (len(rows)+chunks.BLOCK_ROWS-1)//chunks.BLOCK_ROWS:
            raise ValueError('商品行块数量与门店结果不符')
        for kind, key, size in refs:
            if kind != 'ref' or spool.index[key][0] != size:
                raise ValueError('商品行块引用或长度不符')
        if any(size > MAX_COMPARE_BYTES for _,_,size in refs):
            return None
        for start, (_, key, _) in enumerate(refs):
            checkpoint(cancel)
            actual = json.loads(b''.join(spool.raw(key)))
            offset = start*chunks.BLOCK_ROWS
            if (not isinstance(actual,list) or len(actual) != min(len(rows)-offset,chunks.BLOCK_ROWS)
                    or any(not _same_value(row,rows[offset+i]) for i,row in enumerate(actual))):
                raise ValueError('商品行内容、行数或顺序与当前门店不符')
        return Rows(spool,node,revision,work_pack.digest,_identity(head),warehouse,label)
    except (KeyError, ValueError, zlib.error) as exc:
        raise Conflict(f'{label}：{exc}') from exc


def existing(db, reused, day):
    """Remember stored blocks and references in the previous day's draft head."""
    if not reused:
        return set()
    keys = {key for proof,_ in reused.values() for _,key,_ in _refs(proof.node)}
    previous = db.execute('SELECT payload FROM jobs WHERE day=?',(day,)).fetchone()
    required = set()
    if previous and (manifest := chunks.manifest(previous[0])) is not None:
        for s in reused:
            try:
                node = _row_node(manifest['tree'],('results',s,'rows'))
            except KeyError:
                continue  # The previously saved draft need not contain this store.
            if node is not None:
                required.update(ref[1] for ref in _refs(node) if ref[1] in keys)
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='snapshot_chunks'").fetchone():
        required.update(key for key in keys-required
                        if db.execute('SELECT 1 FROM snapshot_chunks WHERE hash=?',(key,)).fetchone())
    return required


def verify(db, work_pack, revision, heads, parents, publications, reused, present, changed_job, cancel):
    """Verify exact targets and SQLite bytes while holding the signing write lock."""
    if not reused:
        return frozenset()
    chunks.schema(db)
    checked = set()
    references = 0
    for head in heads:
        s = head['store']
        if s not in reused:continue
        proof, child_identity = reused[s]
        try:
            checkpoint(cancel)
            targets = []
            if s in parents:targets.append((parents[s],head['digest']))
            if child_identity is not None:
                wh = proof.warehouse
                child = next(h for h in head['warehouse_heads'] if h['warehouse']==wh)
                if (_identity(child),child['child_revision']) != child_identity:
                    raise ValueError('目标仓单修订已变化')
                targets.append((publications[s].get(wh),child['snapshot_digest']))
            proof.check(work_pack,revision,head,targets)
            if not changed_job:
                blob, = db.execute('SELECT payload FROM jobs WHERE day=? AND revision=?',(head['day'],revision)).fetchone()
                saved = chunks.require_manifest(blob)
                saved_node = _row_node(saved['tree'],('results',s,'rows'))
                if saved['digest'] != proof.draft_digest or saved_node != proof.node:
                    raise ValueError('已保存草稿的商品行与来源不符')
            for _, key, size in _refs(proof.node):
                checkpoint(cancel)
                if (key,size) not in checked:
                    proof.spool.verify_row_block(db,key,size,not changed_job or key in present,cancel)
                    checked.add((key,size))
            references += len(targets)*len(_refs(proof.node))
        except (KeyError, ValueError, zlib.error, StopIteration) as exc:
            raise Conflict(f'{proof.location}：{exc}') from exc
    spool = work_pack.chunks.store
    spool.reused_row_orders = len(reused)
    spool.reused_row_blocks = len(checked)
    spool.reused_row_references = references
    return frozenset(checked)


def child_identity(head):
    return _identity(head),head['child_revision']
