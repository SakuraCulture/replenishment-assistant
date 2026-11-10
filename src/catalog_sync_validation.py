"""Read-only catalog validation, shared by source and frozen no-Qt entrypoints."""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import warehouse_catalog as wc
from priority_list import atomic_json


def digest(path):
    from portable_assets import resolve
    with resolve(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def validate(request, progress=lambda _: None, cancel=None):
    if request.get('schema') != 1 or request.get('warehouse') not in wc.WAREHOUSES:
        raise ValueError('商品校验请求缺少明确仓库')
    count = request.get('expected_rows')
    if type(count) is not int or count < 1:
        raise ValueError('缺少平台已核对商品条数')
    source = Path(request['file']).resolve()
    expected = request.get('sha256')
    if not isinstance(expected, str) or len(expected) != 64 or digest(source) != expected:
        raise ValueError('原文件哈希不符，未校验或采用')
    catalog = wc.import_catalog(source, request['warehouse'], progress, cancel)
    if len(catalog['items']) != count or catalog['source_sha256'] != expected:
        raise ValueError('平台条数、文件条数或源文件哈希不一致')
    rows = list(catalog['items'].values())
    states = Counter(str(row.get('商品状态')) for row in rows)
    if set(states) - {'启用', '备用', '禁用'}:
        raise ValueError('全状态商品资料包含未知商品状态，请核对平台模板')
    candidate = Path(request['candidate']).resolve()
    if candidate == source:
        raise ValueError('校验输出不能覆盖原文件')
    if candidate.exists():
        raise FileExistsError('校验候选已存在，请查询原结果或使用新的校验编号')
    atomic_json(candidate, catalog)
    return {'schema': 1, 'status': 'VALIDATED', 'validator': 'catalog.v1',
            'warehouse': catalog['warehouse'], 'source_sha256': expected,
            'rows': len(rows), 'unique_skus': len(rows), 'states': dict(states),
            'missing': {k: sum(r.get(k) in (None, '') for r in rows) for k in wc.REQUIRED},
            'revision': catalog['revision'], 'candidate': str(candidate),
            'candidate_sha256': digest(candidate), 'source': str(source)}


def candidate(receipt):
    if receipt.get('status') != 'VALIDATED' or receipt.get('validator') != 'catalog.v1':
        raise ValueError('商品资料尚未通过当前校验')
    from portable_assets import resolve
    path = resolve(receipt['candidate'])
    if digest(path) != receipt['candidate_sha256']:
        raise ValueError('商品资料校验候选已变化')
    data = wc.validate(json.loads(path.read_text('utf-8')))
    if (data['warehouse'], data['revision'], len(data['items']), data['source_sha256']) != (
            receipt['warehouse'], receipt['revision'], receipt['rows'], receipt['source_sha256']):
        raise ValueError('校验候选与回执不一致')
    if Path(data['source_path']).resolve() != Path(receipt['source']).resolve():
        raise ValueError('校验候选原件路径不一致')
    if digest(data['source_path']) != receipt['source_sha256']:
        raise ValueError('原商品资料在校验后发生变化')
    return data


def cli(argv=None):
    p = argparse.ArgumentParser(); p.add_argument('--request', required=True); p.add_argument('--result', required=True)
    args = p.parse_args(argv); target = Path(args.result)
    try:
        if target.exists():
            raise FileExistsError('结果文件已存在')
        result = validate(json.loads(Path(args.request).read_text('utf-8')))
        result['qt_imported'] = any(k.startswith('PySide6') for k in sys.modules)
        atomic_json(target, result)
        return 0
    except Exception as exc:
        if not target.exists():
            atomic_json(target, {'status': 'FAILED', 'error': str(exc)})
        return 1


if __name__ == '__main__':
    raise SystemExit(cli())
