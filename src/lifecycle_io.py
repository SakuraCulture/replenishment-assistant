"""Explicit whole-document adapters; inventory units, immutable Excel originals."""
import io
from datetime import date, datetime
from decimal import Decimal, localcontext
from pathlib import Path
import openpyxl
import purchase_io as base
from runtime_support import checkpoint
from export_progress import RowProgress

STAGES = ('return', 'payment', 'receipt')
TITLES = {'return': '门店回传单', 'payment': '门店付款单', 'receipt': '门店实际收货单'}
QUANTITIES = {'return': '门店确认数量', 'payment': '付款单商品数量', 'receipt': '实际收货数量'}
DOCNUM = {'return': '回传单号', 'payment': '付款单号', 'receipt': '收货单号'}
DOCDATE = {'return': '回传日期', 'payment': '付款日期', 'receipt': '收货日期'}
COMMON = ['补货单号', '签发修订', '门店编码', '供货仓', '商品编码', '商品名称', '数量单位', '折合库存单位系数', '换算依据']

def fields(stage):
    return COMMON + [QUANTITIES[stage], DOCNUM[stage]] + ([] if stage=='payment' else [DOCDATE[stage]]) + (['实际金额', '币种'] if stage == 'payment' else []) + ['备注']

def required(stage):
    return ['商品编码', QUANTITIES[stage], '数量单位'] + ([DOCNUM[stage], '实际金额'] if stage == 'payment' else [DOCNUM[stage], DOCDATE[stage]] if stage == 'receipt' else [])

def input_fields(stage):
    """Business-facing mapping; internal identity and units remain compatibility data."""
    return ['补货单号','商品编码',QUANTITIES[stage]] + ([DOCNUM[stage],'实际金额','币种'] if stage=='payment' else [DOCNUM[stage],DOCDATE[stage]] if stage=='receipt' else []) + ['商品名称','备注']

def field_key(value):
    text=str(value or '').strip()
    for suffix in ('（必填）','（选填）','（系统带入）','(必填)','(选填)'):
        if text.endswith(suffix):return text[:-len(suffix)].strip()
    return text

def auto_mapping(stage, headers):
    aliases={'门店确认数量':['补货数量'],'供货仓':['所属仓','仓']}
    headers=[field_key(h) for h in headers]
    result={}
    for field in fields(stage):
        hits=[i for i,h in enumerate(headers) if h==field]
        if not hits:hits=[i for i,h in enumerate(headers) if h in aliases.get(field,[])]
        result[field]=hits[0] if len(hits)==1 else None
    return result

PAYMENT_HEADERS = ['内部订单号','线上订单号','店铺名称','买家账号','下单时间','平台站点','商品编码','商品名称','箱规','数量','商品单价','商品金额','辅助码']

def payment_specs(info):
    result=[]
    for sheet in info['sheets']:
        hs=sheet['headers']
        if not all(hs.count(h)==1 for h in PAYMENT_HEADERS):continue
        aliases={'商品编码':'商品编码','商品名称':'商品名称','付款单商品数量':'数量','付款单号':'内部订单号','实际金额':'商品金额'}
        result.append({**info,**sheet,'mapping':{f:hs.index(aliases[f]) if f in aliases else None for f in fields('payment')},'defaults':{'数量单位':'库存单位'},'adapter':'paid_order'})
    return result

def inspect(paths, progress=lambda _: None, cancel=None):
    infos = base.inspect(paths, progress, cancel)
    for f in infos:
        checkpoint(cancel)
        raw = Path(f['path']).read_bytes()
        if base.digest(raw) != f['sha']: raise ValueError('文件在读取期间变化')
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, keep_links=False)
        try: f['properties'] = {p.name: p.value for p in wb.custom_doc_props}
        finally: wb.close()
    return infos

def revision(value):
    if value in (None, ''): return None
    text = str(value).strip().lstrip('Vv')
    try: n = Decimal(text)
    except Exception: raise ValueError('签发修订须为正整数或V001') from None
    if not n.is_finite() or n <= 0 or n != n.to_integral_value(): raise ValueError('签发修订须为正整数或V001')
    return int(n)

def scan(stage, specs, progress=lambda _: None, cancel=None):
    names = fields(stage); rows = []; files = []
    for i, spec in enumerate(specs):
        checkpoint(cancel); p = Path(spec['path']); raw = p.read_bytes()
        if base.digest(raw) != spec['sha']: raise ValueError('映射后文件变化，请重新读取：' + p.name)
        files.append({k: spec[k] for k in ('path', 'name', 'sha')})
        mapping = dict(spec['mapping']); defaults = spec.get('defaults', {})
        number_only=spec.get('order_number_only',False)
        if number_only:mapping['签发修订']=None
        columns = [v for v in mapping.values() if v is not None]
        if len(columns) != len(set(columns)): raise ValueError('一个源列不能同时映射多个字段')
        for key in required(stage):
            if mapping.get(key) is None and defaults.get(key) in (None, ''): raise ValueError('缺少字段映射：' + key)
        book = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=False, keep_links=False); cached = None
        try:
            ws = book[spec['sheet']]; ws.reset_dimensions(); iterator = ws.values
            if list(next(iterator, ())) != spec['headers']: raise ValueError('表头已变化')
            for n, cells in enumerate(iterator, 2):
                checkpoint(cancel)
                if not any(v not in (None, '') for v in cells): continue
                try:
                    d = {f: cells[c] if c is not None and c < len(cells) else defaults.get(f) for f in names for c in [mapping.get(f)]}
                    if number_only:d['签发修订']=None
                    if any(isinstance(v, str) and v.startswith('=') for v in d.values()):
                        if cached is None: cached = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True, keep_links=False)
                        values = next(cached[spec['sheet']].iter_rows(min_row=n, max_row=n, values_only=True))
                        for f, v in list(d.items()):
                            if isinstance(v, str) and v.startswith('='):
                                c = mapping[f]; d[f] = values[c] if c < len(values) else None
                                if d[f] is None: raise ValueError(f + '公式无缓存值，请在Excel计算保存后重试')
                    raw_qty=d[QUANTITIES[stage]]
                    blank=raw_qty is None or isinstance(raw_qty,str) and not raw_qty.strip()
                    qty = None if blank else base.number(raw_qty); unit = str(d['数量单位'] or '').strip()
                    factor = base.number(d['折合库存单位系数'], True); basis = str(d['换算依据'] or '').strip()
                    if not unit: raise ValueError('数量单位不能为空')
                    if factor is not None and (Decimal(factor) <= 0 or not basis): raise ValueError('换算系数须大于0且提供依据')
                    if unit == '库存单位' and factor is not None and Decimal(factor) != 1: raise ValueError('库存单位的换算系数只能为1')
                    if unit != '库存单位' and factor is None: raise ValueError('非库存单位须提供明确换算系数及依据')
                    with localcontext() as ctx:
                        ctx.prec = max(28, len(Decimal(qty or '0').as_tuple().digits) + len(Decimal(factor or 1).as_tuple().digits))
                        converted = None if qty is None else base.number(Decimal(qty) * Decimal(factor or 1))
                    day = None if stage=='payment' else d[DOCDATE[stage]]
                    if isinstance(day, datetime): day = day.date().isoformat()
                    elif isinstance(day, date): day = day.isoformat()
                    elif day not in (None, ''): day = str(day).strip(); date.fromisoformat(day)
                    else: day = None
                    if stage == 'receipt' and day is None: raise ValueError(DOCDATE[stage] + '不能为空')
                    doc = base.code(d[DOCNUM[stage]], stage == 'return')
                    props = spec.get('properties', {}); signed = base.code(d['补货单号'], True)
                    rev = revision(d['签发修订'])
                    if props.get('签发修订索引') and not number_only:
                        import json
                        index=json.loads(props['签发修订索引']);embedded=revision(index.get(signed))
                        if embedded is None or rev is not None and rev!=embedded:raise ValueError('单号与合并文件修订标识不一致')
                        rev=embedded
                    if props.get('修订'):
                        if props.get('补货单号') != signed: raise ValueError('单号与导出文件修订标识不一致')
                        if not number_only:
                            embedded = revision(props['修订'])
                            if rev is not None and rev != embedded: raise ValueError('行修订与文件修订不一致')
                            rev = embedded
                    wh = str(d['供货仓'] or '').strip()
                    if wh and wh not in ('河北仓', '山东仓'): raise ValueError('供货仓须为河北仓或山东仓')
                    currency = str(d.get('币种') or '').strip()
                    rows.append({'sku': base.code(d['商品编码']), 'name': str(d['商品名称'] or ''), 'signed_number': signed, 'revision': rev, 'store': base.code(d['门店编码'], True), 'warehouse': wh, 'document_number': doc, 'document_date': day, 'quantity': qty, 'unit': unit, 'factor': factor, 'conversion_basis': basis, 'base_quantity': converted, 'amount': base.number(d.get('实际金额'), stage != 'payment'), 'currency': currency, 'note': d['备注'], 'source': {'sha': spec['sha'], 'file': p.name, 'sheet': ws.title, 'row': n}, 'raw_headers': spec['headers'], 'raw_values': list(cells)})
                    rows[-1]['quantity_origin']='blank' if blank else 'zero' if Decimal(qty)==0 else 'value'
                    if spec.get('adapter')=='paid_order':
                        rows[-1]['source_order']={h:cells[spec['headers'].index(h)] if spec['headers'].index(h)<len(cells) else None for h in PAYMENT_HEADERS}
                        rows[-1]['adapter']='paid_order'
                except (ValueError, ArithmeticError) as exc: raise ValueError(f'{p.name} / {ws.title} 第{n}行：{exc}') from exc
                if n % 200 == 0: progress(RowProgress(f'读取{TITLES[stage]} {p.name} 第{n}行', i, len(specs)))
        finally:
            book.close()
            if cached: cached.close()
        if base.adapter.digest(p) != spec['sha']: raise ValueError('读取期间文件变化：' + p.name)
        progress(RowProgress(f'已校验文件 {i+1}/{len(specs)}', i+1, len(specs)))
    if not rows: raise ValueError('没有商品明细；请核对文件，或在目标订单中明确确认本环节整单为0')
    return {'stage': stage, 'rows': rows, 'files': list({f['sha']: f for f in files}.values())}

def template(stage, path):
    import os, tempfile
    from openpyxl.styles import Font, PatternFill
    from openpyxl.comments import Comment
    target=Path(path)
    if target.exists():raise FileExistsError('目标文件已存在，不覆盖')
    columns=PAYMENT_HEADERS if stage=='payment' else input_fields(stage)
    mandatory={'补货单号','商品编码',QUANTITIES[stage]} | ({DOCNUM[stage],DOCDATE[stage]} if stage=='receipt' else set())
    if stage=='payment':mandatory={'内部订单号','商品编码','数量','商品金额'}
    labels=columns if stage=='payment' else [f+('（必填）' if f in mandatory else '（选填）') for f in columns]
    book=openpyxl.Workbook();ws=book.active;ws.title=TITLES[stage];ws.append(labels);ws.freeze_panes='A2'
    for field,cell in zip(columns,ws[1]):
        cell.font=Font(bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='126C63' if field in mandatory else '657B83');ws.column_dimensions[cell.column_letter].width=28
        cell.comment=Comment(('必填字段。' if field in mandatory else '选填字段。')+('数量列须存在；空白/缺行在整单核对确认后按0。' if field in (QUANTITIES[stage],'数量') else '单张订单详情导入时可由系统带入；批量须填写。' if field=='补货单号' else ''),'补货助手')
    for n in range(2,202):
        for i,field in enumerate(columns,1):
            if '编码' in field or '单号' in field or field in ('内部订单号','线上订单号','买家账号','辅助码'):ws.cell(n,i).number_format='@'
    notes=[('关联','仅用补货单号关联当前有效签发。签发修订、门店和仓库由系统维护；不填、不选择。'),('只填本环节','上一环节数量、签发建议、差额由系统读取计算，无需抄写。回传也可直接修改原签发单的补货数量。'),('必填与选填','绿底为必填，灰底为选填；付款沿用原表，不需补填原表缺少的选填信息。'),('补货单号','单张订单详情导入时可以不填，由所选订单带入；批量导入须提供每行补货单号。原签发单已带号，不用重填。'),('数量','数量列必须存在；明确0、空白和缺行在确认完整单据后均按0，处理来源分别保留。未导入整单不是0。'),('单位','与签发/回传相同的库存单位，不乘箱规，无需填写单位、折算系数。'),('来源单据','回传无需另编回传单号或填回传日期。付款保留内部订单号及商品金额；收货单号/收货日期是实际业务信息，仍须提供。'),('编码','商品编码及补货单号按文本填写，保留前导零；每仓单每商品一行。'),('上游变化','导入时关联当前签发；预览后订单变化则拒绝保存，重新核对。历史事实不删除。')]
    help_sheet=book.create_sheet('填写说明');help_sheet.append(['项目','说明'])
    for row in notes:help_sheet.append(row)
    for f in columns:help_sheet.append((f,'必填；数量空白可确认按0' if f in (QUANTITIES[stage],'数量') else '必填；单订单详情可由系统带入' if f=='补货单号' else '必填' if f in mandatory else '选填'))
    help_sheet.column_dimensions['A'].width=28;help_sheet.column_dimensions['B'].width=110
    fd,tmp=tempfile.mkstemp(dir=target.parent,suffix='.xlsx');os.close(fd)
    try:book.save(tmp);os.link(tmp,target)
    finally:book.close();Path(tmp).unlink(missing_ok=True)
