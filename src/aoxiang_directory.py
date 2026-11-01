"""Reusable AX store directory. Header-based import, exact codes, immutable source copy."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

import openpyxl
from input_adapter import identifier
from runtime_support import checkpoint
from aoxiang_jobs import encoded, now

SUPPLIERS = {'山东仓': 'ZXC2025', '河北仓': '2078246'}


def file_hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def directory_version(items):
    # Preserve order; a changed directory order is a different reusable source version.
    return hashlib.sha256(encoded([{'code': i['code'], 'name': i['name']} for i in items]).encode('utf-8')).hexdigest()


def read_directory(path, *, progress=lambda _: None, cancel=None):
    path = Path(path).resolve()
    before = file_hash(path)
    book = openpyxl.load_workbook(path, read_only=True, data_only=True, keep_links=False)
    candidates = []
    try:
        for sheet in book:
            checkpoint(cancel)
            sheet.reset_dimensions()
            for row_number, row in enumerate(sheet.iter_rows(values_only=True), 1):
                if row_number > 50:
                    break
                header = [re.sub(r'\s+', '', str(v or '')) for v in row]
                if '门店编码' in header and '门店名称' in header:
                    if header.count('门店编码') != 1 or header.count('门店名称') != 1:
                        raise ValueError(f'{sheet.title} 第{row_number}行存在重复门店表头')
                    candidates.append((sheet.title, row_number, header.index('门店编码'), header.index('门店名称')))
                    break
        if len(candidates) != 1:
            raise ValueError('需要且只能有一个含“门店编码、门店名称”的数据表，列位置可变')
        sheet_name, header_row, code_col, name_col = candidates[0]
        sheet = book[sheet_name]
        items, by_code, duplicates = [], {}, 0
        for number, row in enumerate(sheet.iter_rows(min_row=header_row + 1, values_only=True), header_row + 1):
            checkpoint(cancel)
            if not any(v is not None and v != '' for v in row):
                continue
            code, error = identifier(row[code_col] if len(row) > code_col else None)
            raw_name = row[name_col] if len(row) > name_col else None
            if error or code != code.strip():
                raise ValueError(f'{sheet_name} 第{number}行：{error or "门店编码含前后空格，请修正原表"}')
            if not isinstance(raw_name, str) or not raw_name.strip():
                raise ValueError(f'{sheet_name} 第{number}行：门店名称缺失或不是文本')
            name = raw_name.strip()
            if code in by_code:
                if by_code[code]['name'] != name:
                    raise ValueError(f'{sheet_name} 第{number}行：同一编码 {code} 对应不同名称，不能选择第一条')
                by_code[code]['source_rows'].append(number)
                duplicates += 1
                continue
            item = {'code': code, 'name': name, 'source_rows': [number]}
            items.append(item)
            by_code[code] = item
            if len(items) % 100 == 0:
                progress(f'已读取 {len(items)} 家门店')
    finally:
        book.close()
    checkpoint(cancel)
    if file_hash(path) != before:
        raise ValueError('门店目录在读取期间变化，请重新导入')
    if not items:
        raise ValueError('门店目录没有有效门店')
    return {'schema': 1, 'version': directory_version(items), 'items': items, 'duplicate_rows': duplicates,
            'source': {'path': str(path), 'name': path.name, 'sha256': before, 'sheet': sheet_name, 'header_row': header_row},
            'saved_at': None}


def validate(data):
    if not isinstance(data, dict) or data.get('schema') != 1 or not isinstance(data.get('items'), list) or not data['items']:
        raise ValueError('门店目录格式不完整')
    seen = set()
    for item in data['items']:
        if not isinstance(item.get('code'), str) or not item['code'].strip() or item['code'] in seen or not isinstance(item.get('name'), str) or not item['name'].strip():
            raise ValueError('门店目录编码或名称无效、重复')
        seen.add(item['code'])
    if directory_version(data['items']) != data.get('version'):
        raise ValueError('门店目录内容与版本不一致，请重新导入')
    return data


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.aoxiang-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


class Directory:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.path = self.folder / 'store-directory.json'

    def load(self):
        return validate(json.loads(self.path.read_text(encoding='utf-8'))) if self.path.exists() else None

    def save(self, data, *, progress=lambda _: None, cancel=None):
        validate(data)
        checkpoint(cancel)
        source = Path(data['source']['path'])
        expected = data['source']['sha256']
        if file_hash(source) != expected:
            raise ValueError('核对后原门店目录发生变化，未保存')
        retained = self.folder / 'sources/store-directory' / (expected + '.xlsx')
        retained.parent.mkdir(parents=True, exist_ok=True)
        if not retained.exists():
            fd, tmp = tempfile.mkstemp(dir=retained.parent, suffix='.tmp')
            os.close(fd)
            try:
                shutil.copyfile(source, tmp)
                if file_hash(tmp) != expected:
                    raise ValueError('门店目录留存校验失败')
                checkpoint(cancel)
                os.link(tmp, retained)
            finally:
                Path(tmp).unlink(missing_ok=True)
        if file_hash(retained) != expected:
            raise ValueError('已留存门店原件被改动，未采用')
        checkpoint(cancel)
        saved = {**data, 'source': {**data['source'], 'retained_path': str(retained.resolve())}, 'saved_at': now()}
        atomic_json(self.path, saved)
        progress(f'已保存 {len(saved["items"])} 家门店；平台编码核验在选店连接时执行')
        return saved

    def suppliers(self):
        path = self.folder / 'suppliers.json'
        result = json.loads(path.read_text(encoding='utf-8')) if path.exists() else dict(SUPPLIERS)
        if set(result) != set(SUPPLIERS) or any(not isinstance(v, str) or not v.strip() for v in result.values()):
            raise ValueError('两仓供应商编码必须为非空文本')
        return result

    def save_suppliers(self, values):
        if set(values) != set(SUPPLIERS) or any(not isinstance(v, str) or not v.strip() for v in values.values()):
            raise ValueError('两仓供应商编码必须为非空文本')
        atomic_json(self.folder / 'suppliers.json', values)
