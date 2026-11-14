"""Disk-spooled formula values for the application's streaming XLSX exports.

Only generated worksheet formula caches are patched. The template formulas and
all unrelated ZIP members remain unchanged; memory is bounded by a worksheet row.
"""
import json
import math
import os
import re
import shutil
import tempfile
import zipfile
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from runtime_support import checkpoint

# et_xmlfile (the packaged serializer) emits styled empty cells as <c ... />.
# They must not consume the following nonempty formula cell as their contents.
CELL = re.compile(rb'<c\b[^>]*\br="([A-Z]+[1-9][0-9]*)"[^>]*(?<!/)>.*?</c>', re.S)
ROW = re.compile(rb'<row\b[^>]*\br="([1-9][0-9]*)"')
VALUE = re.compile(rb'<v(?:\s[^>]*)?>.*?</v>|<v\s*/>', re.S)
TYPE = re.compile(rb'\s+t="[^"]*"')


class FormulaCache:
    def __init__(self, folder, cancel=None):
        self.temporary = tempfile.TemporaryDirectory(prefix='.analytics-cache-', dir=folder)
        self.folder = Path(self.temporary.name)
        self.cancel, self.files = cancel, {}

    def __enter__(self): return self

    def __exit__(self, *args):
        for item in self.files.values(): item['stream'].close()
        self.temporary.cleanup()

    def add(self, sheet, coordinate, value):
        match = re.fullmatch(r'[A-Z]+([1-9][0-9]*)', coordinate)
        if not match: raise ValueError('Invalid generated formula coordinate')
        if sheet not in self.files:
            path = self.folder / ('sheet-%d.jsonl' % len(self.files))
            self.files[sheet] = {'path': path, 'stream': path.open('w', encoding='utf-8', newline='\n'), 'last_row': 0}
        item = self.files[sheet]; row = int(match[1])
        if row < item['last_row']: raise ValueError('Formula cache must follow generated row order')
        item['last_row'] = row
        if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
            if not math.isfinite(value): raise ValueError('Nonfinite formula cache value')
            kind, text = 'number', str(value)
        elif isinstance(value, str): kind, text = 'string', value
        else: raise ValueError('Formula cache must be a number or explicit unavailable text')
        item['stream'].write(json.dumps({'cell': coordinate, 'row': row, 'kind': kind, 'value': text}, ensure_ascii=False)+'\n')

    def _worksheet(self, source, target, cache_path):
        with cache_path.open(encoding='utf-8') as cache:
            def next_value():
                line = cache.readline(); return json.loads(line) if line else None
            current = next_value(); buffer = b''
            while True:
                checkpoint(self.cancel)
                block = source.read(65536)
                if not block: break
                buffer += block
                while b'</row>' in buffer:
                    row_xml, buffer = buffer.split(b'</row>', 1); row_xml += b'</row>'
                    match = ROW.search(row_xml)
                    if not match: raise ValueError('Generated worksheet row not found')
                    row = int(match[1]); values = {}
                    if current and current['row'] < row: raise ValueError('Formula cache row was not found')
                    while current and current['row'] == row:
                        if current['cell'] in values: raise ValueError('Duplicate formula cache coordinate')
                        values[current['cell']] = current; current = next_value()
                    def replace_cell(match):
                        coordinate = match[1].decode('ascii'); value = values.pop(coordinate, None)
                        if value is None: return match[0]
                        xml = match[0]
                        if b'<f' not in xml: raise ValueError('Cache does not refer to a formula cell: '+coordinate)
                        head, body = xml.split(b'>', 1); head = TYPE.sub(b'', head)
                        if value['kind'] == 'string': head += b' t="str"'
                        cached = ('<v>'+escape(value['value'])+'</v>').encode('utf-8')
                        if VALUE.search(body): body, count = VALUE.subn(lambda _: cached, body)
                        else: body, count = body.replace(b'</c>', cached+b'</c>'), 1
                        if count != 1: raise ValueError('Unexpected generated formula value count')
                        return head+b'>'+body
                    target.write(CELL.sub(replace_cell, row_xml))
                    if values: raise ValueError('Formula cell missing from generated row')
                    checkpoint(self.cancel)
            target.write(buffer)
            if current: raise ValueError('Formula cache extends beyond generated worksheet')

    def apply(self, source):
        for item in self.files.values(): item['stream'].close()
        source = Path(source); output = self.folder / 'cached.xlsx'
        ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        relationship = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
        with zipfile.ZipFile(source) as original:
            rels = {r.get('Id'): r.get('Target') for r in ET.fromstring(original.read('xl/_rels/workbook.xml.rels'))}
            worksheets = {}
            for sheet in ET.fromstring(original.read('xl/workbook.xml')).find('m:sheets', ns):
                target = rels[sheet.get(relationship)]
                path = target.lstrip('/') if target.startswith('/') else 'xl/'+target
                if sheet.get('name') in self.files: worksheets[path] = self.files[sheet.get('name')]['path']
            if len(worksheets) != len(self.files): raise ValueError('Cached worksheet identity missing')
            with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED) as revised:
                for member in original.infolist():
                    checkpoint(self.cancel)
                    with original.open(member) as incoming, revised.open(member, 'w', force_zip64=True) as outgoing:
                        if member.filename in worksheets: self._worksheet(incoming, outgoing, worksheets[member.filename])
                        else: shutil.copyfileobj(incoming, outgoing, 65536)
        checkpoint(self.cancel); os.replace(output, source)
