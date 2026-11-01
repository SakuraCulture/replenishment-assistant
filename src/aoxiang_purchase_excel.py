"""Fill only the native template input rows, preserving all other ZIP members byte-for-byte."""
import copy
import hashlib
import json
import os
import posixpath
import re
import tempfile
import zipfile
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

from aoxiang_directory import file_hash
from aoxiang_purchase import fingerprint
from runtime_support import checkpoint

MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
NS = {'s': MAIN}


def template_receipt(receipt, contract_path):
    contract = json.loads(Path(contract_path).read_text(encoding='utf-8'))
    if (receipt.get('status') != 'SUPPORTED' or receipt.get('contractVersion') != contract['version']
            or receipt.get('schema') != contract['schema']):
        raise ValueError('采购模板未通过当前字段契约核验')
    if file_hash(receipt['file']) != receipt['sha256']:
        raise ValueError('采购模板在核验后发生变化')
    return contract


def text_value(value):
    value = str(value)
    if any(ord(c) < 32 and c not in '\t\n\r' for c in value):
        raise ValueError('采购内容含不能写入Excel的控制字符')
    return escape(value)


def build_input_sheet(raw, plan, spec, *, note=None):
    root = ET.fromstring(raw)
    data = root.find('s:sheetData', NS)
    if data is None:
        raise ValueError('模板缺少数据区')
    start = spec['inputStartRow']
    samples = [r for r in data if int(r.attrib['r']) >= start]
    if [int(r.attrib['r']) for r in samples] != [v['row'] for v in spec['sampleRows']]:
        raise ValueError('模板样例行与已核验契约不符')
    styles = {}
    if samples:
        styles = {re.sub(r'\d+$', '', c.attrib['r']): c.attrib.get('s') for c in samples[0]}
    source = raw.decode('utf-8')
    match = re.search(r'<(?P<p>[A-Za-z_][\w.-]*:)?sheetData(?:\s[^>]*)?>(?P<body>[\s\S]*?)</(?P=p)?sheetData>', source)
    if not match:
        raise ValueError('模板数据区编码未支持，未改写')
    prefix = match['p'] or ''
    row_pattern = r'<(?P<p>[A-Za-z_][\w.-]*:)?row\b(?P<attrs>[^>]*?)(?:/>|>[\s\S]*?</(?P=p)?row>)'
    removed = []
    def drop_sample(m):
        row = re.search(r'\br\s*=\s*[\'"](\d+)[\'"]', m['attrs'])
        if not row:
            raise ValueError('模板行缺少编号')
        if int(row[1]) >= start:
            removed.append(int(row[1]))
            return ''
        return m[0]
    body = re.sub(row_pattern, drop_sample, match['body'])
    if removed != [v['row'] for v in spec['sampleRows']]:
        raise ValueError('模板样例清除范围不符')
    for index, row in enumerate(plan['rows'], start):
        values = [plan['head']['store'], plan['supplier_code'], row['sku'], row['quantity'], None, row['amount'],
                  None, row['name'], None, plan['head']['number'] if note is None else note, '否', None]
        attrs = {k: v for k, v in (samples[0].attrib if samples else {}).items() if k not in ('r', 'spans')}
        attrs['r'] = str(index)
        cells = []
        for column, value in zip('ABCDEFGHIJKL', values):
            if value is None:
                continue
            style = '' if styles.get(column) is None else ' s=' + quoteattr(styles[column])
            ref = f'{column}{index}'
            if column in ('D', 'F'):
                # Decimal text is written directly, with no intermediate binary float or price rounding.
                decimal = Decimal(value)
                if (not decimal.is_finite() or decimal < 0 or len(decimal.normalize().as_tuple().digits) > 15
                        or Decimal(str(float(decimal))) != decimal):
                    raise ValueError('数量或金额超出Excel可保留的数值精度，需核对，未静默改量：' + row['sku'])
                numeric = format(decimal, 'f')
                cells.append(f'<{prefix}c r="{ref}"{style}><{prefix}v>{numeric}</{prefix}v></{prefix}c>')
            else:
                cells.append(f'<{prefix}c r="{ref}"{style} t="inlineStr"><{prefix}is><{prefix}t xml:space="preserve">{text_value(value)}</{prefix}t></{prefix}is></{prefix}c>')
        body += '<' + prefix + 'row ' + ' '.join(k + '=' + quoteattr(v) for k, v in attrs.items()) + '>' + ''.join(cells) + '</' + prefix + 'row>'
    rewritten = source[:match.start('body')] + body + source[match.end('body'):]
    # The supplied native template declares A1 despite populated rows. Set the actual extent.
    last = start + len(plan['rows']) - 1
    rewritten, count = re.subn(r'(<(?:[A-Za-z_][\w.-]*:)?dimension\b[^>]*\bref=)[\'"][^\'"]*[\'"]', lambda m: m[1] + quoteattr(f'A1:L{last}'), rewritten, count=1)
    if count != 1:
        raise ValueError('模板范围声明未支持')
    ET.fromstring(rewritten)
    return rewritten.encode('utf-8')


def generate(plan, receipt, contract_path, target, *, progress=lambda _: None, cancel=None, note=None):
    checkpoint(cancel)
    if fingerprint(plan) != plan['fingerprint']:
        raise ValueError('采购预览内容已变化')
    if not plan['rows']:
        raise ValueError('没有采购商品')
    identity = receipt.get('identity') or {}
    if identity.get('accountId') != plan['account_binding']['account_id'] or identity.get('merchantCode') != plan['account_binding']['merchant_code']:
        raise ValueError('采购模板所属商家与付款采购预览不一致')
    contract = template_receipt(receipt, contract_path)
    limit = receipt.get('uiProductLimit')
    if limit is not None and (type(limit) is not int or limit < 1 or len(plan['rows']) > limit):
        raise ValueError('商品数量超出平台单次上限或上限无效，本期不自动拆单')
    target = Path(target)
    if target.exists():
        raise FileExistsError('采购文件目标已存在，不能覆盖原采购依据')
    spec = next(s for s in contract['schema']['sheets'] if s['state'] == 'visible')
    fd, temp = tempfile.mkstemp(dir=target.parent, suffix='.xlsx')
    os.close(fd)
    try:
        with zipfile.ZipFile(receipt['file']) as original:
            book = ET.fromstring(original.read('xl/workbook.xml'))
            sheet = next(s for s in book.findall('s:sheets/s:sheet', NS) if s.attrib['name'] == spec['name'])
            rels = ET.fromstring(original.read('xl/_rels/workbook.xml.rels'))
            rel = next(r for r in rels if r.attrib['Id'] == sheet.attrib['{' + REL + '}id'])
            destination = rel.attrib['Target']
            member = posixpath.normpath(destination.lstrip('/') if destination.startswith('/') else 'xl/' + destination)
            if not member.startswith('xl/worksheets/'):
                raise ValueError('模板工作表位置未支持')
            rewritten = build_input_sheet(original.read(member), plan, spec, note=note)
            with zipfile.ZipFile(temp, 'w') as output:
                output.comment = original.comment
                for entry in original.infolist():
                    checkpoint(cancel)
                    output.writestr(copy.copy(entry), rewritten if entry.filename == member else original.read(entry))
        checkpoint(cancel)
        if file_hash(receipt['file']) != receipt['sha256']:
            raise ValueError('生成期间原模板发生变化，未采用')
        with open(temp, 'r+b') as stream:
            os.fsync(stream.fileno())
        os.link(temp, target)
    finally:
        Path(temp).unlink(missing_ok=True)
    progress(f'采购文件已生成：{len(plan["rows"])} 款，金额 {plan["total_amount"]} 元；仅建单，保持待提交')
    return {'schema': 1, 'file': str(target.resolve()), 'sha256': file_hash(target), 'bytes': target.stat().st_size,
            'plan_fingerprint': plan['fingerprint'], 'template_sha256': receipt['sha256'],
            'template_receipt': {k: copy.deepcopy(receipt[k]) for k in ['attemptId', 'checkedAt', 'source', 'transport', 'recordFile', 'contractVersion', 'identity', 'importSubmissions'] if k in receipt},
            'rows': len(plan['rows']), 'amount': plan['total_amount'], 'modified_zip_member': member}
