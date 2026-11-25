"""Current eligibility views and confirmed changes to exact refusal records."""
import copy,json,os,re,sqlite3,tempfile
from contextlib import closing
from itertools import islice
from pathlib import Path
import admission
from runtime_support import checkpoint

QUAL_HEADERS=['门店编码','门店名称','当前有效状态','登记状态','核验人','核验日期','到期日','拒收中款数','备注']
REF_HEADERS=['门店编码','门店名称','商品编码','商品名称','拒收原因','到期日','当前状态']
QUAL_REPORT='二类医疗资质'
REFUSAL_REPORT='门店拒收清单'
REFUSAL_ACTIONS={'release':'解除','edit':'修改','delete':'删除'}
REFUSAL_EDIT_HEADERS=[*REF_HEADERS[:4],'原拒收原因','新拒收原因','原到期日','新到期日','原状态','新状态']
REFUSAL_END_HEADERS=[*REF_HEADERS[:6],'原状态','操作结果']
REFUSAL_RELEASE_IMPORT_HEADERS=['原表行号','门店编码','商品编码','核对结果','当前状态','门店名称','商品名称','拒收原因','到期日']


def qualification_state(value,day):
    if value.get('expires') and value['expires']<day:return '已到期'
    return '有效' if value.get('state')=='已核验' else value.get('state','待核验')


def refusal_state(value,day):
    return '已解除' if not value.get('active',True) else '已到期' if value.get('expires') and value['expires']<day else '拒收中'


def record_name(record):
    if record.get('name'):return str(record['name'])
    candidates=[record.get('qualification',{}),*record.get('refusals',{}).values()]
    for item in candidates:
        name=item.get('source',{}).get('original',{}).get('门店名称')
        if name:return str(name)
    return ''


def load(path,known=None,progress=lambda _:None,cancel=None,*,day=None):
    """Single SQLite read transaction, plus optional local directory, no database creation."""
    path=Path(path);day=day or admission.today();names=dict(known or {});records={};warnings=[]
    checkpoint(cancel);progress('正在读取门店准入现状…')
    if path.exists():
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
            db.execute('BEGIN');tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'stores' in tables:
                for code,raw in db.execute('SELECT code,data FROM stores ORDER BY rowid'):
                    checkpoint(cancel);r=json.loads(raw)
                    if not names.get(code):names[code]=r.get('name','')
            if 'store_admission' in tables:
                for code,raw in db.execute('SELECT store,payload FROM store_admission ORDER BY rowid'):
                    checkpoint(cancel);records[code]=json.loads(raw)
    from aoxiang_directory import load_for_settings
    try:
        directory=load_for_settings(path.parent)
        for item in (directory or {}).get('items',[]):
            if not names.get(item['code']):names[item['code']]=item['name']
    except (OSError,ValueError,KeyError,TypeError) as exc:warnings.append('门店目录未能读取，以下仍展示作业、参数和准入记录中的门店：'+str(exc))
    for code,r in records.items():
        if not names.get(code):names[code]=record_name(r)
    qualifications=[];refusals=[]
    for n,(code,name) in enumerate(names.items(),1):
        checkpoint(cancel);r=records.get(code,{})
        try:admission.validate(r)
        except Exception as exc:raise ValueError(f'门店 {code} 准入资料异常，未将其当作有效资料：{exc}') from exc
        q=r.get('qualification',{});active=0
        for sku,item in r.get('refusals',{}).items():
            state=refusal_state(item,day);active+=state=='拒收中'
            refusals.append([code,name,sku,item.get('name',''),item['reason'],item.get('expires') or '手动解除',state])
        qualifications.append([code,name,qualification_state(q,day),q.get('state','未登记'),q.get('reviewer',''),q.get('verified_on') or '',q.get('expires') or '',active,q.get('note','')])
        if n%100==0:progress(f'已整理 {n}/{len(names)} 家门店')
    return {'day':day,'names':names,'qualification':qualifications,'refusals':refusals,'warnings':warnings,'records':records}


def prepare_refusal_change(data, keys, action, fields=None, progress=lambda _: None, cancel=None):
    """Preview and freeze exact store/SKU changes against the displayed snapshot."""
    label = REFUSAL_ACTIONS[action]
    selected = list(dict.fromkeys(tuple(key) for key in keys))
    if not selected:
        raise ValueError(f'请先勾选要{label}的门店商品')
    if action == 'edit':
        fields = dict(fields or {})
        if not fields or set(fields) - {'reason', 'expires'}:
            raise ValueError('请选择要修改的拒收原因或到期日')
        if 'reason' in fields:
            fields['reason'] = str(fields['reason'] or '').strip()
            if not fields['reason']:
                raise ValueError('请填写门店拒收原因')
        if 'expires' in fields:
            fields['expires'] = admission.date_value(fields['expires'])
    old, new, rows = {}, {}, []
    day = data['day']
    for n, (store, sku) in enumerate(selected, 1):
        checkpoint(cancel)
        record = data['records'].get(store, {})
        item = record.get('refusals', {}).get(sku)
        if not item or action == 'release' and not item.get('active', True):
            raise ValueError(f'记录已不存在或当前状态无法{label}，请刷新后重选：{store} / {sku}')
        if action == 'edit' and all(item.get(key) == value for key, value in fields.items()):
            continue
        if store not in old:
            old[store] = copy.deepcopy(record)
            new[store] = copy.deepcopy(record)
        changed = new[store]['refusals'][sku]
        identity = [store, data['names'].get(store, ''), sku, item.get('name', '')]
        expiry = item.get('expires') or '手动解除'
        if action == 'edit':
            changed.update(fields)
            rows.append([*identity, item['reason'], changed['reason'], expiry, changed.get('expires') or '手动解除',
                         refusal_state(item, day), refusal_state(changed, day)])
        else:
            if action == 'delete':
                del new[store]['refusals'][sku]
            else:
                changed['active'] = False
            rows.append([*identity, item['reason'], expiry, refusal_state(item, day),
                         '从清单删除' if action == 'delete' else '已解除'])
        if n % 200 == 0:
            progress(f'正在核对{label}范围 {n}/{len(selected)} 条…')
    return {'action': action, 'expected': old, 'values': new, 'rows': rows, 'count': len(rows), 'stores': list(old)}


def commit_refusal_change(path, prepared, progress=lambda _: None, cancel=None):
    checkpoint(cancel)
    result = {key: prepared[key] for key in ('count', 'stores')}
    if result['count']:
        label = REFUSAL_ACTIONS[prepared['action']]
        progress(f'正在提交 {result["count"]} 条{label}记录，涉及 {len(result["stores"])} 家门店…')
        admission.save(path, prepared['values'], prepared['expected'])
        progress(f'已{label} {result["count"]} 条，正在刷新列表…')
    return result


def prepare_release(data, keys, progress=lambda _: None, cancel=None):
    return prepare_refusal_change(data, keys, 'release', progress=progress, cancel=cancel)


def commit_release(path, prepared, progress=lambda _: None, cancel=None):
    return commit_refusal_change(path, prepared, progress, cancel)


def prepare_release_import(path, source, known=None, progress=lambda _: None, cancel=None):
    """Read exact store/SKU pairs and freeze only the matched changes for review."""
    from openpyxl import load_workbook
    from import_templates import TEMPLATES
    from store_parameter_import import code_value, digest

    _, template_sheet, required, _ = TEMPLATES['store_refusal_release']
    progress('正在读取批量解除文件…')
    sha = digest(source)
    book = load_workbook(source, read_only=True, data_only=False)
    try:
        sheets = {ws.title: ws for ws in book.worksheets}
        sheet = sheets.get(template_sheet) or sheets.get(REFUSAL_REPORT) or next(iter(sheets.values()), None)
        if sheet is None:
            raise ValueError('文件没有可读工作表')
        sheet.reset_dimensions()
        rows = enumerate(sheet.iter_rows(), 1)
        for number, cells in islice(rows, 20):
            checkpoint(cancel)
            headers = [str(cell.value or '').strip() for cell in cells]
            if all(name in headers for name in required):
                if any(headers.count(name) > 1 for name in required):
                    raise ValueError(f'「{sheet.title}」第 {number} 行的编码表头重复，请保留各一列')
                indices = [headers.index(name) for name in required]
                break
        else:
            raise ValueError(f'「{sheet.title}」前 20 行内需要「门店编码」和「商品编码」两列表头')

        data = load(path, known, progress, cancel)
        preview, keys, seen = [], [], set()
        for number, cells in rows:
            checkpoint(cancel)
            if all(cell.value in (None, '') for cell in cells):
                continue
            codes, invalid = [], []
            for name, index in zip(required, indices):
                cell = cells[index] if index < len(cells) else None
                value = cell.value if cell is not None else None
                try:
                    if cell is None or cell.data_type == 'e' or (
                        isinstance(value, (int, float)) and cell.number_format not in ('General', '0', '@')
                    ):
                        raise ValueError()
                    codes.append(code_value(cell))
                except ValueError:
                    codes.append('' if value is None else str(value))
                    invalid.append(name)

            store, sku = key = tuple(codes)
            item = {} if invalid else data['records'].get(store, {}).get('refusals', {}).get(sku, {})
            state = refusal_state(item, data['day']) if item else ''
            if invalid:
                result = '跳过 · 编码无效（' + '、'.join(invalid) + '）'
            elif key in seen:
                result = '跳过 · 表内重复'
            elif not item:
                result = '跳过 · 未找到拒收记录'
            elif state == '已解除':
                result = '跳过 · 已解除'
            else:
                keys.append(key)
                result = '将解除'
            if not invalid:
                seen.add(key)
            preview.append([number, store, sku, result, state, data['names'].get(store, ''),
                            item.get('name', ''), item.get('reason', ''),
                            (item.get('expires') or '手动解除') if item else ''])
            if len(preview) % 200 == 0:
                progress(f'已核对 {len(preview)} 条文件记录…')
    finally:
        book.close()
    if digest(source) != sha:
        raise ValueError('读取期间文件已变化，请重新导入')
    if not preview:
        raise ValueError('表头下没有需要核对的记录，请填写编码')
    prepared = prepare_release(data, keys, progress, cancel) if keys else None
    return {'source': source, 'sha': sha, 'sheet': sheet.title, 'rows': preview, 'keys': keys, 'prepared': prepared}


def commit_release_import(path, plan, progress=lambda _: None, cancel=None):
    from store_parameter_import import digest

    checkpoint(cancel)
    if not plan['prepared']:
        raise ValueError('没有可解除的拒收记录')
    if digest(plan['source']) != plan['sha']:
        raise ValueError('预览后文件已变化，请重新导入')
    return commit_release(path, plan['prepared'], progress, cancel)


def select_rows(rows,kind,state='全部',text='',focus=None):
    terms=text.lower().split();status_col=2 if kind=='qualification' else 6
    return [r for r in rows if (state=='全部' or r[status_col]==state)
            and (focus is None or (r[0],None if kind=='qualification' else r[2]) in focus)
            and all(t in ' '.join(str(v) for v in r).lower() for t in terms)]


def selection_codes(raw):
    """Parse pasted codes as text, preserving @ and all leading zeros."""
    from purchase_io import code
    return frozenset(code(term) for term in re.split(r'[\s,，;；、]+', raw.strip()) if term)


def select_refusals(rows, state, focus, store='', product='', reason='', store_codes=(), sku_codes=()):
    store_terms, product_terms, reason_terms = (value.casefold().split() for value in (store, product, reason))
    result = []
    for row in select_rows(rows, 'refusals', state, focus=focus):
        if (store_codes and row[0] not in store_codes) or (sku_codes and row[2] not in sku_codes):
            continue
        if (all(term in f'{row[0]} {row[1]}'.casefold() for term in store_terms)
                and all(term in f'{row[2]} {row[3]}'.casefold() for term in product_terms)
                and all(term in str(row[4]).casefold() for term in reason_terms)):
            result.append(row)
    return result


def export(path,kind,rows,scope,progress=lambda _:None,cancel=None):
    """Export precisely the displayed snapshot, not a fresh query with different filters."""
    from openpyxl import Workbook
    from openpyxl.styles import Font,PatternFill
    def append_text(sheet,row):
        sheet.append(row)
        for cell in sheet[sheet.max_row]:
            if isinstance(cell.value,str):
                cell.data_type='s'
                cell.number_format='@'

    target=Path(path)
    qualification=kind=='qualification'
    if qualification:
        from import_templates import make_workbook
        wb=make_workbook('store_qualification',blank_rows=0)
        editable=wb.active
        fields=[cell.value for cell in editable[1]]
        ws=wb.create_sheet(QUAL_REPORT)
    else:
        wb=Workbook()
        ws=wb.active
        ws.title=REFUSAL_REPORT
    ws.append(QUAL_HEADERS if qualification else REF_HEADERS)
    ws.freeze_panes='C2'
    for index,row in enumerate(rows,1):
        checkpoint(cancel)
        append_text(ws,row)
        if qualification:
            values=dict(zip(QUAL_HEADERS,row))
            values['资质状态']=values['登记状态']
            if values['登记状态']=='未登记':
                values={key:values[key] for key in ('门店编码','门店名称')}
            append_text(editable,[values.get(field) for field in fields])
        if index%200==0:progress(f'正在导出 {index}/{len(rows)} 条记录…')
    if qualification:editable.auto_filter.ref=editable.dimensions
    for cell in ws[1]:
        cell.font=Font(bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='126C63');ws.column_dimensions[cell.column_letter].width=22
    ws.auto_filter.ref=ws.dimensions
    note=wb.create_sheet('查询范围')
    for k,v in scope.items():
        note.append([k,str(v)])
        for cell in note[note.max_row]:cell.data_type='s'
    detail='修改门店资质页后可直接导回；二类医疗资质页记录本次查询现状。' if qualification else '文件中只保留需要解除的行后，可从「导入批量解除」读取。隐藏的行也会被读取。'
    note.append(['说明','当前状态按查询日判断；到期当天仍有效。'+detail])
    note.column_dimensions['A'].width=22;note.column_dimensions['B'].width=90
    fd,tmp=tempfile.mkstemp(dir=target.parent,suffix='.xlsx');os.close(fd)
    try:progress(f'保存 {len(rows)} 条查询结果…');wb.save(tmp);checkpoint(cancel);os.replace(tmp,target)
    finally:wb.close();Path(tmp).unlink(missing_ok=True)
    return str(target)
