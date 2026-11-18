"""Spool formula values and insert them as each worksheet is packaged."""
import json
import math
import os
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
from io import BufferedWriter
from pathlib import Path
from xml.sax.saxutils import escape

from openpyxl.writer.excel import ExcelWriter

from runtime_support import checkpoint

# et_xmlfile (the packaged serializer) emits styled empty cells as <c ... />.
# They must not consume the following nonempty formula cell as their contents.
CELL = re.compile(rb'<c\b[^>]*\br="([A-Z]+[1-9][0-9]*)"[^>]*(?<!/)>.*?</c>', re.S)
ROW = re.compile(rb'<row\b[^>]*\br="([1-9][0-9]*)"')
VALUE = re.compile(rb'<v(?:\s[^>]*)?>.*?</v>|<v\s*/>', re.S)
TYPE = re.compile(rb'\s+t="[^"]*"')
BUFFER_BYTES = 1024 * 1024


class _FormulaArchive(zipfile.ZipFile):
    def __init__(self, path, worksheets, write_cached_sheet):
        super().__init__(path, 'x', compression=zipfile.ZIP_DEFLATED)
        self.worksheets = worksheets
        self.write_cached_sheet = write_cached_sheet

    def write(self, filename, arcname=None, compress_type=None, compresslevel=None):
        cache_path = self.worksheets.pop(arcname, None)
        if cache_path is None:
            return super().write(filename, arcname, compress_type, compresslevel)
        member = zipfile.ZipInfo.from_file(filename, arcname)
        member.compress_type = self.compression if compress_type is None else compress_type
        member._compresslevel = self.compresslevel if compresslevel is None else compresslevel
        with open(filename, 'rb', buffering=BUFFER_BYTES) as source:
            with BufferedWriter(self.open(member, 'w', force_zip64=True), BUFFER_BYTES) as target:
                self.write_cached_sheet(source, target, cache_path)


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
            self.files[sheet] = {'path': path, 'stream': path.open('w', encoding='utf-8', newline='\n', buffering=BUFFER_BYTES), 'last_row': 0}
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
        with cache_path.open(encoding='utf-8', buffering=BUFFER_BYTES) as cache:
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

    def save(self, book, destination):
        """Build one complete archive before replacing the destination staging file."""
        checkpoint(self.cancel)
        if not book.write_only:
            raise ValueError('Formula cache requires a streaming workbook')
        for item in self.files.values(): item['stream'].close()
        # ExcelWriter assigns worksheet paths by their one-based workbook order.
        worksheets = {f'xl/worksheets/sheet{index}.xml': self.files[sheet.title]['path']
                      for index, sheet in enumerate(book.worksheets, 1) if sheet.title in self.files}
        if len(worksheets) != len(self.files):
            raise ValueError('Cached worksheet identity missing')
        output = self.folder / 'workbook.xlsx'
        with _FormulaArchive(output, worksheets, self._worksheet) as archive:
            book.properties.modified = datetime.now(timezone.utc).replace(tzinfo=None)
            ExcelWriter(book, archive).write_data()
            if worksheets:
                raise ValueError('Cached worksheet identity missing')
        checkpoint(self.cancel)
        os.replace(output, destination)
