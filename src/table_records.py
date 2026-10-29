"""Table boundary shared by file IO and the former temporary-XLSX bridges.

The bridge projection explicitly preserves openpyxl 3.1.5 write/read semantics:
empty text becomes None, numbers use Excel's 16 significant digit serialization,
date serials are read back to millisecond precision. This is NOT model coercion.
Actual source formulas are still read data_only by the unchanged Excel reader.
"""
from collections import defaultdict
import openpyxl
from openpyxl.cell import WriteOnlyCell
from openpyxl.cell._writer import _set_attributes
from openpyxl.compat.strings import safe_string
from openpyxl.worksheet._reader import _cast_number
from openpyxl.styles.numbers import is_date_format,is_timedelta_format
from openpyxl.utils.datetime import from_excel


def table_records(header,values,sheet,required):
    """Convert already-read cells; retain row positions and duplicate-header rules."""
    if not header:raise ValueError('文件为空')
    header=[str(x).strip() if x is not None else '' for x in header]
    positions=defaultdict(list)
    for i,h in enumerate(header):positions[h].append(i)
    missing=[h for h in required if h not in positions]
    repeated=[h for h in required if len(positions.get(h,[]))>1]
    if missing:raise ValueError('缺少必需表头：'+','.join(missing))
    if repeated:raise ValueError('必需表头重复：'+','.join(repeated))
    rows=[]
    for rn,vs in enumerate(values,2):
        if not any(x is not None and x!='' for x in vs):continue
        row={h:vs[idx[0]] if idx[0]<len(vs) else None for h,idx in positions.items() if len(idx)==1}
        row['_row']=rn;rows.append(row)
    return rows,{'sheet':sheet,'rows':len(rows),'headers':header,
                 'duplicate_unused_headers':[h for h,idx in positions.items() if len(idx)>1]}


def require_headers(info,required):
    """Same validation when an already materialized table is used again."""
    table_records(info['headers'],(),info['sheet'],required)


def bridge_cell(ws,value,force_text=True):
    """Project one former temporary workbook cell, without saving/reading a file."""
    c=WriteOnlyCell(ws,value)
    if isinstance(value,str) and force_text:c.data_type='s'
    value,attrs=_set_attributes(c)
    if value is None or value=='':return None
    if c.data_type=='f':return None  # no cache in an application-generated formula
    # lxml emits CR as a character reference; the bundled etree writer emits a
    # literal CR, which XML readback normalizes to LF. Keep each old environment.
    if c.data_type=='s':return value if openpyxl.LXML else value.replace('\r','\n')
    if c.data_type=='b':return bool(value)
    if attrs.get('t')=='n':
        text=safe_string(value)
        if not text:return None
        value=_cast_number(text)
        if is_date_format(c.number_format):
            try:value=from_excel(value,ws.parent.epoch,timedelta=is_timedelta_format(c.number_format))
            except (OverflowError,ValueError):value='#VALUE!'
    return value


def bridge_table(headers,records,sheet,required=(),*,header_text=False,checkpoint=lambda:None):
    """Internal only: values that previously passed through an empty 1900 workbook.

    An empty in-memory workbook supplies Cell's binding/validation context only.
    No ZIP, worksheet XML, temporary file, or workbook read/write is performed.
    """
    book=openpyxl.Workbook();ws=book.active;ws.title=sheet
    try:
        # Writer omits trailing None cells; an empty string has an explicit cell.
        last=next((i+1 for i in range(len(headers)-1,-1,-1) if headers[i] is not None),0)
        header=[bridge_cell(ws,x,header_text) for x in headers[:last]]
        def values():
            for row in records:
                checkpoint()
                yield [bridge_cell(ws,row.get(h)) for h in headers]
        return table_records(header,values(),sheet,required)
    finally:book.close()
