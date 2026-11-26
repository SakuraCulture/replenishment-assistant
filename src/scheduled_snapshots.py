"""Store-scoped views of the immutable inputs and results of a scheduled run."""
from collections.abc import Mapping, MutableMapping, Sequence
from itertools import islice
from pathlib import Path
from dataclasses import dataclass


@dataclass(frozen=True)
class Reference:
    path: Path
    run: str
    name: str
    digest: str

    def read(self):
        from scheduled_replenishment import Repository
        return Repository(self.path).value(self.run, self.name, expected=self.digest)


def projection(result, focus):
    import result_semantics
    import review_amounts
    import warehouse_orders
    rows = result['rows']
    try:warehouses=warehouse_orders.summaries(result, focus)
    except ValueError:warehouses=None
    return {'summary': result['summary'], 'budget': result['budget'],
            'risk_count': sum(bool(result_semantics.inbound_risk(row)) for row in rows),
            'money': {k: str(v) if k in review_amounts.MONEY_KEYS else v
                      for k, v in review_amounts.summarize(rows).items()},
            'warehouses': warehouses,
            'row_warehouses': sorted({row['AU'] for row in rows if row.get('AU')})}


class Results(MutableMapping):
    """Load on selection. Keep edited stores as owned overlays."""
    def __init__(self, refs, heads, overrides=None):
        self.refs = refs
        self.heads = heads
        self.overrides = {} if overrides is None else overrides
        self.cached_key = self.cached_value = None

    def __getstate__(self):
        return self.refs, self.heads, self.overrides

    def __setstate__(self, state):
        self.__init__(*state)

    def __iter__(self):
        return iter(dict.fromkeys((*self.refs, *self.overrides)))

    def __len__(self):
        return len(self.refs.keys() | self.overrides.keys())

    def __contains__(self, key):
        return key in self.overrides or key in self.refs

    def __getitem__(self, key):
        if key in self.overrides:
            return self.overrides[key]
        if self.cached_key != key:
            self.release()
            self.cached_value = self.refs[key].read()
            self.cached_key = key
        return self.cached_value

    def __setitem__(self, key, value):
        self.overrides[key] = value
        if self.cached_key == key:
            self.release()

    def __delitem__(self, key):
        if key not in self:
            raise KeyError(key)
        self.refs.pop(key, None)
        self.heads.pop(key, None)
        self.overrides.pop(key, None)
        if self.cached_key == key:
            self.release()

    def subset(self, keys):
        return Results({s: self.refs[s] for s in keys if s in self.refs},
                       {s: self.heads[s] for s in keys if s in self.heads},
                       {s: self.overrides[s] for s in keys if s in self.overrides})

    def head(self, key):
        return self.heads.get(key) if key not in self.overrides else None

    def release(self):
        self.cached_key = self.cached_value = None


def head_for(results, key):
    return results.head(key) if isinstance(results, Results) else None


def fields_for(results, key):
    return head_for(results, key) or results.get(key)


def subset(results, keys):
    return results.subset(keys) if isinstance(results, Results) else {s: results[s] for s in keys}


class OrderedResults(Mapping):
    """Merge the checked stores and the remaining draft in its original order."""
    def __init__(self, keys, checked, remaining):
        self.keys_in_order = keys
        self.checked = checked
        self.remaining = remaining

    def __len__(self):
        return len(self.keys_in_order)

    def __iter__(self):
        return iter(self.keys_in_order)

    def __getitem__(self, key):
        return self.checked[key] if key in self.checked else self.remaining[key]


def ordered(keys, checked, remaining):
    if isinstance(checked, Results) and isinstance(remaining, Results):
        refs,heads,edits={**remaining.refs,**checked.refs},{**remaining.heads,**checked.heads},{**remaining.overrides,**checked.overrides}
        return Results({s:refs[s] for s in keys if s in refs},heads,{s:edits[s] for s in keys if s in edits})
    return OrderedResults(keys, checked, remaining)


class Rows(Sequence):
    """Sequence with the same original per-store rows and a bounded iterator."""
    def __init__(self, path, parts):
        self.path, self.parts = path, parts
        self.size = sum(count for _, count in parts.values())

    def __len__(self):
        return self.size

    def __iter__(self):
        return self.for_stores(self.parts)

    def for_stores(self, stores):
        for key, (ref, _) in self.parts.items():
            if key in stores:
                data = ref.read()
                for segment in self.path:
                    data = data[segment]
                yield from data

    def blocks(self, size):
        """Keep each immutable store's row block boundaries on re-archive."""
        for store in self.parts:
            remaining=self.for_stores((store,))
            while group:=list(islice(remaining,size)):
                yield group

    def __getitem__(self, index):
        if isinstance(index, slice):
            first, stop, step = index.indices(len(self))
            if step < 0:
                return list(self)[index]
            return list(islice(self, first, max(first, stop), step))
        if index < 0:
            index += len(self)
        if 0 <= index < len(self):
            return next(islice(self, index, index + 1))
        raise IndexError(index)


def input_head(loaded):
    intake = loaded['intake']
    return {'name': next(iter(loaded['stores'].values())), 'config': loaded['config'],
            'files': intake['files'], 'headers': intake['headers'], 'hashes': loaded['hashes'],
            'source_hashes': intake['hashes'], 'blocked': loaded['blocked'], 'duplicates': intake['duplicates'],
            'rows': len(loaded['normalized']), 'selected': len(intake['selected']), 'conflicts': len(intake['conflicts']),
            'local': sum(row['_source']=='地采商品' for row, _ in loaded['normalized']),
            'warehouse_rows': {wh: len(rows) for wh, rows in loaded.get('warehouse_rows', {}).items()}}


def input_view(repo, run, catalogs, digests):
    from warehouse_catalog import metadata
    rid = run['id']
    entries = {item['code']: item['input'] for item in run['stores'] if 'input' in item}
    if not entries:
        return None
    refs = {s: Reference(repo.path, rid, 'input:'+s, digests['input:'+s]) for s in entries}
    stores = {s: entry['name'] for s, entry in entries.items()}
    files = list({item['path']: item for entry in entries.values() for item in entry['files']}.values())
    headers = sorted({h for entry in entries.values() for h in entry['headers']})
    hashes = {p: h for entry in entries.values() for p, h in entry['hashes'].items()}
    source_hashes = {p: h for entry in entries.values() for p, h in entry['source_hashes'].items()}
    config = dict(next(iter(entries.values()))['config'])
    config['category_profiles'] = {k: v for entry in entries.values() for k, v in entry['config']['category_profiles'].items()}
    def rows(path, read_count):
        return Rows(path, {s: (refs[s], count) for s, entry in entries.items() if (count := read_count(entry))})
    return {'stores': stores, 'config': config, 'hashes': hashes, 'sales': [item['path'] for item in files],
            'normalized': rows(('normalized',), lambda entry: entry['rows']),
            'warehouse_rows': {wh: rows(('warehouse_rows', wh), lambda entry: entry['warehouse_rows'].get(wh, 0)) for wh in catalogs},
            'warehouses': list(catalogs), 'catalogs': {wh: metadata(catalog) for wh, catalog in catalogs.items()},
            'blocked': {s: value for entry in entries.values() for s, value in entry['blocked'].items()},
            'intake': {'files': files, 'stores': stores, 'headers': headers, 'hashes': source_hashes,
                       'products': None, 'products_snapshot': None, 'catalogs': catalogs,
                       'selected': rows(('intake', 'selected'), lambda entry: entry['selected']),
                       'conflicts': rows(('intake', 'conflicts'), lambda entry: entry['conflicts']),
                       'duplicates': sum(entry['duplicates'] for entry in entries.values())},
            'row_counts': {s: [entry['rows'], entry['local']] for s, entry in entries.items()}}
