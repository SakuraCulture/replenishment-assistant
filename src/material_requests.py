"""Versioned store requests for warehouse consumables, independent of the model."""
import hashlib
import io
import uuid
from contextlib import closing
from pathlib import Path

import openpyxl

import input_adapter
import jst_allocation
import work_repository as wr
from app_defaults import WAREHOUSES
from runtime_support import checkpoint


KIND = 'material'
HEADERS = ('门店编码', '供货仓', '商品编码', '申请数量', '备注', '申请编号', '申请版本')


def schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS material_requests(
        id TEXT PRIMARY KEY, store TEXT NOT NULL, warehouse TEXT NOT NULL,
        created TEXT NOT NULL, current INTEGER NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS material_request_versions(
        id TEXT NOT NULL, version INTEGER NOT NULL, source_key TEXT UNIQUE,
        saved TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(id,version))''')


def exists(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='material_requests'").fetchone())


def eligibility(item):
    if item is None:
        return '该仓资料没有此商品编码'
    if str(item.get('分类') or '').strip() != '门店耗材':
        return '产品分类须为门店耗材'
    if str(item.get('商品状态') or '').strip() not in ('启用', '备用'):
        return '商品状态须为启用或备用'
    return ''


def identifier(value):
    code, issue = input_adapter.identifier(value)
    if issue:
        raise ValueError(issue)
    if code != code.strip() or any(ord(char) < 32 for char in code):
        raise ValueError('编码含首尾空格或控制字符，请核对原文')
    return code


def normalize_line(line):
    from purchase_io import number

    value = jst_allocation.number(number(line['quantity']), '申请数量')
    return {'sku': identifier(line['sku']), 'quantity': jst_allocation.text(value),
            'note': str(line.get('note') or ''), 'row': line.get('row')}


def save(db, store, warehouse, lines, *, request_id=None, expected=None, source=None):
    store = identifier(store)
    if warehouse not in WAREHOUSES:
        raise ValueError('供货仓须为山东仓或河北仓')
    # Use the same stored identifiers that order tracking uses. Names are display only.
    if not db.execute('SELECT 1 FROM stores WHERE code=?', (store,)).fetchone():
        raise ValueError('未找到门店编码 '+store+'，请先维护门店资料')
    rows = [normalize_line(line) for line in lines]
    if not rows or len({line['sku'] for line in rows}) != len(rows):
        raise ValueError('同一申请至少有一行，各商品编码只填一次')
    schema(db)
    source_key = wr.dumps([source['sha'], store, warehouse, request_id]) if source else None
    if source_key:
        prior = db.execute('SELECT id,version FROM material_request_versions WHERE source_key=?', (source_key,)).fetchone()
        if prior:
            return prior
    stamp = wr.now()
    if request_id:
        prior = db.execute('SELECT store,warehouse,current FROM material_requests WHERE id=?', (request_id,)).fetchone()
        if prior != (store, warehouse, expected):
            raise wr.Conflict('物料申请版本、门店或供货仓已变化，请重新打开原申请')
        from material_orders import _claimed
        amount = {row['sku']:jst_allocation.number(row['quantity'],'申请数量') for row in rows}
        for (owner,sku), qty in _claimed(db).items():
            if owner==request_id and amount.get(sku,0)<qty:
                raise wr.Conflict(f'{sku} 已分配 {jst_allocation.text(qty)}，请沿原物料仓单核对后再减量')
        version = expected + 1
        db.execute('UPDATE material_requests SET current=? WHERE id=?', (version, request_id))
    else:
        request_id, version = uuid.uuid4().hex, 1
        db.execute('INSERT INTO material_requests VALUES(?,?,?,?,?)', (request_id, store, warehouse, stamp, version))
    payload = wr.dumps({'lines': rows, 'source': source})
    digest = hashlib.sha256(payload.encode('utf-8')).hexdigest()
    db.execute('INSERT INTO material_request_versions VALUES(?,?,?,?,?,?)',
               (request_id, version, source_key, stamp, digest, payload))
    return request_id, version


def read(db, request_id=None):
    if not exists(db):
        return []
    sql = '''SELECT m.id,m.store,m.warehouse,m.created,v.version,v.digest,v.payload
             FROM material_requests m JOIN material_request_versions v ON v.id=m.id AND v.version=m.current'''
    args = ()
    if request_id is not None:
        sql += ' WHERE m.id=?'
        args = (request_id,)
    result = []
    for request, store, warehouse, created, version, digest, payload in db.execute(sql+' ORDER BY m.rowid', args):
        if hashlib.sha256(payload.encode('utf-8')).hexdigest() != digest:
            raise ValueError('物料申请保存内容校验失败 '+request)
        result.append(dict(id=request, store=store, warehouse=warehouse, created=created,
                           version=version, digest=digest, **wr.loads(payload)))
    return result


def scan(path, cancel=None):
    path = Path(path)
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    book = openpyxl.load_workbook(io.BytesIO(raw), data_only=True, read_only=True, keep_links=False)
    try:
        page = book['物料需求'] if '物料需求' in book else book.active
        cells = page.values
        titles = list(next(cells, ()))
        if any(titles.count(title) != 1 for title in HEADERS[:4]):
            raise ValueError('表头须各有一列门店编码、供货仓、商品编码和申请数量')
        positions = {title: titles.index(title) for title in HEADERS if title in titles}
        if any(titles.count(title) != 1 for title in positions):
            raise ValueError('物料需求表头有重复列')
        groups, issues = {}, []
        for row, values in enumerate(cells, 2):
            checkpoint(cancel)
            if not any(value is not None and value != '' for value in values):
                continue
            item = {title: values[i] for title, i in positions.items()}
            try:
                store = identifier(item['门店编码'])
                warehouse = str(item['供货仓'] or '').strip()
                if warehouse not in WAREHOUSES:
                    raise ValueError('供货仓须为山东仓或河北仓')
                reference = str(item.get('申请编号') or '').strip() or None
                revision = item.get('申请版本')
                expected = None
                if reference:
                    value = jst_allocation.number(revision, '申请版本')
                    if value < 1 or value != value.to_integral_value():
                        raise ValueError('申请版本须为正整数')
                    expected = int(value)
                line = normalize_line(dict(sku=item['商品编码'], quantity=item['申请数量'], note=item.get('备注'), row=row))
                key = (store, warehouse, reference)
                group = groups.setdefault(key, dict(store=store, warehouse=warehouse, request_id=reference,
                                                     expected=expected, lines=[]))
                if group['expected'] != expected:
                    raise ValueError('同一申请出现不同版本')
                group['lines'].append(line)
            except ValueError as exc:
                issues.append(f'第 {row} 行 {exc}')
        return {'source': {'name': path.name, 'sha': sha, 'sheet': page.title}, 'raw': raw,
                'groups': list(groups.values()), 'issues': issues}
    finally:
        book.close()


def import_scanned(path, scanned, cancel=None):
    """The review reports structural issues before any accepted group is saved."""
    from order_lifecycle import begin

    if scanned['issues']:
        raise ValueError('\n'.join(scanned['issues']))
    if not scanned['groups']:
        raise ValueError('表中没有物料需求')
    source = scanned['source']
    if hashlib.sha256(scanned['raw']).hexdigest() != source['sha']:
        raise ValueError('物料申请原件与预读摘要不同')
    with closing(wr.Repository.open_existing(path).connect()) as db, db:
        begin(db, cancel)
        db.execute('''CREATE TABLE IF NOT EXISTS source_files(
            sha TEXT PRIMARY KEY,name TEXT NOT NULL,saved TEXT NOT NULL,content BLOB NOT NULL)''')
        db.execute('INSERT OR IGNORE INTO source_files VALUES(?,?,?,?)',
                   (source['sha'], source['name'], wr.now(), scanned['raw']))
        results = []
        for group in scanned['groups']:
            checkpoint(cancel)
            results.append(save(db, **group, source=source))
        return results


def write_template(path, request=None):
    book = openpyxl.Workbook()
    page = book.active
    page.title = '物料需求'
    page.append(HEADERS)
    page.freeze_panes = 'D2'
    for col in ('A', 'C', 'F'):
        page.column_dimensions[col].number_format = '@'
    if request:
        for row in request['lines']:
            page.append((request['store'], request['warehouse'], row['sku'], row['quantity'], row['note'],
                         request['id'], request['version']))
            for col in (1, 2, 3, 4, 5, 6):
                page.cell(page.max_row, col).data_type = 's'
    with Path(path).open('xb') as stream:
        book.save(stream)
