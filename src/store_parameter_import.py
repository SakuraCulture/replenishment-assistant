"""Store parameter spreadsheet patches. Pure validation; no model or database writes."""
import copy
import hashlib
import math
import os
import tempfile
from pathlib import Path
from openpyxl import load_workbook, Workbook
from openpyxl.styles import Font, PatternFill, Alignment
import parameter_schema as schema
import store_defaults
import restock_policy as policy
from app_defaults import WAREHOUSES
from runtime_support import Cancelled
from parameter_view import flatten, display_path

FIXED = {'depth', 'lead', 'extreme_days'}
WEIGHTS = {'alpha': '趋势敏感度', 'w1': '最近7天权重', 'w2': '此前7天权重'}
COMMON = ['门店编码', '门店名称', '主仓', '从仓', '预算方式', '预算金额', '启用一箱例外']+list(policy.LABELS.values())
MINIMUM = COMMON + [w + n for w in WAREHOUSES for n in ('备货天数', '到货天数')]


def checkpoint(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled()


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def field(header):
    text = str(header or '').strip()
    aliases = {'仓库/门店编码': '门店编码', '仓库/门店名称': '门店名称',
               '主供仓': '主仓', '从供仓': '从仓', '门店预算': '预算金额'}
    text = aliases.get(text, text)
    if text in COMMON:
        return (text,)
    for wh in WAREHOUSES:
        for i, names in enumerate((('备货天数', '备货深度'), ('到货天数', '到货时间'))):
            if text in [wh + n for n in names]:
                return ('warehouses', wh, i)
    for key, (name, _, _) in schema.HELP.items():
        if text in (key, name):
            return ('parameters', key)
    if text.startswith('权重/') and '/' in text[3:]:
        name, label = text[3:].rsplit('/', 1)
        key = next((k for k, v in WEIGHTS.items() if label in (k, v)), None)
        if key:
            return ('weight_profiles', name, key)
    if text.startswith('分类映射/') and text[5:]:
        return ('category_profiles', text[5:])
    if text.startswith('分类参数/') and '/' in text[5:]:
        name,label=text[5:].rsplit('/',1)
        key=next((k for k in schema.PROFILE_KEYS if label in (k,schema.HELP[k][0])),None)
        if key:return ('profile_parameters',name,key)
    raise ValueError('无法识别表头：' + text + '；请使用模板字段说明中的名称')


def number(value):
    if isinstance(value, bool):
        raise ValueError('须填写非负数字，不能填布尔值')
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise ValueError('须填写非负数字') from None
    if not math.isfinite(n) or n < 0:
        raise ValueError('须填写有限的非负数字')
    return n


def code_value(cell):
    value = cell.value
    if cell.data_type == 'f':
        raise ValueError('门店编码不能使用公式')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value) or value < 0 or int(value) != value or len(str(int(value))) > 15:
            raise ValueError('数字编码无效或超过15位，请用原始文本编码')
        return str(int(value))
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value.strip()):
        raise ValueError('门店编码必填，须为文本或完整整数')
    return value.strip()


def read(paths, progress=lambda _: None, cancel=None):
    from parameter_tables import read as read_classified
    return read_classified(paths,progress,cancel)


def read_legacy(paths, progress=lambda _: None, cancel=None, *, _book=None, _before=None):
    """Read once, preserve file/sheet/row order, reject ambiguous patches atomically."""
    result = {'rows': [], 'sources': {}}
    seen = {}
    for path in dict.fromkeys(str(Path(p).resolve()) for p in paths):
        checkpoint(cancel)
        before = _before if _before is not None else digest(path)
        progress('读取门店参数：' + Path(path).name)
        wb = _book if _book is not None else load_workbook(path, read_only=True, data_only=False)
        found = False
        try:
            for ws in wb:
                iterator = ws.iter_rows()
                fields = None
                for rn, cells in enumerate(iterator, 1):
                    checkpoint(cancel)
                    if fields is None:
                        if rn > 20:
                            break
                        if not any(str(c.value or '').strip() in ('门店编码', '仓库/门店编码') for c in cells):
                            continue
                        fields = []
                        for c in cells:
                            fields.append(field(c.value) if c.value not in (None, '') else None)
                        nonempty = [f for f in fields if f]
                        if len(set(nonempty)) != len(nonempty):
                            raise ValueError(f'{ws.title}：有重复含义的表头')
                        found = True
                        continue
                    if all(c.value in (None, '') for c in cells):
                        continue
                    source = f'{Path(path).name} / {ws.title} / 第{rn}行'
                    try:
                        values = {}
                        for f, cell in zip(fields, cells):
                            if not f:
                                if cell.value not in (None, ''):
                                    raise ValueError('数据列缺少表头')
                                continue
                            v = cell.value
                            if v is None or isinstance(v, str) and not v.strip():
                                continue
                            if cell.data_type in ('f', 'e'):
                                raise ValueError('参数不能使用公式或Excel错误值，请粘贴为值')
                            values[f] = code_value(cell) if f == ('门店编码',) else v.strip() if isinstance(v, str) else v
                        code = values.pop(('门店编码',), None)
                        if not code:
                            raise ValueError('门店编码必填')
                        if code in seen:
                            raise ValueError('门店编码重复：' + code + '；首次在' + seen[code])
                        seen[code] = source
                        result['rows'].append({'code': code, 'name': str(values.pop(('门店名称',), '')), 'values': values, 'source': source})
                    except ValueError as exc:
                        raise ValueError(source + '：' + str(exc)) from exc
                    if rn % 100 == 0:
                        progress(f'{Path(path).name} / {ws.title}：已检查{rn-1}行')
            if not found:
                raise ValueError(Path(path).name + '：前20行未找到“门店编码”表头')
        finally:
            wb.close()
        if before != digest(path):
            raise ValueError('读取期间文件变化，请保存后重试：' + path)
        result['sources'][path] = before
    if not result['rows']:
        raise ValueError('没有可导入的门店记录；请在模板中填写门店编码和要修改的参数')
    return result


def base(preset=None, ui=None):
    p = copy.deepcopy(preset or {'id': 'standard', 'name': '标准补货', 'version': 1, 'builtin': True, 'config': schema.standard()})
    return {'preset': p, 'ui': copy.deepcopy(ui or {'mode': 0, 'amount': '', 'primary': None, 'secondary': None, 'selected': True})}


def patch(row, original):
    value = copy.deepcopy(original)
    c, ui = value['preset']['config'], value['ui']
    if any(f[0] not in ('主仓','从仓','预算方式','预算金额','category_profiles') for f in row['values']):value['preset'].pop('implicit_default',None)
    for f, raw in row['values'].items():
        if f[0] in ('主仓', '从仓'):
            v = str(raw).strip()
            if v not in (*WAREHOUSES, '无', '无从仓', '未指定'):
                raise ValueError('主从仓只能填写河北仓、山东仓、无从仓或未指定')
            ui['primary' if f[0] == '主仓' else 'secondary'] = v if v in WAREHOUSES else None
        elif f == ('预算方式',):
            if raw not in ('未指定', '不限预算', '限定预算'):
                raise ValueError('预算方式须为未指定、不限预算或限定预算')
            ui['mode'] = ('未指定', '不限预算', '限定预算').index(raw)
        elif f == ('预算金额',):
            ui['amount'] = format(number(raw), '.15g')
            if ('预算方式',) not in row['values']:
                ui['mode'] = 2
        elif f == ('启用一箱例外',):
            if raw not in (True, False, 1, 0, '是', '否', '启用', '停用', '1', '0'):
                raise ValueError('启用一箱例外须为是/否或1/0')
            c['exception'] = raw in (True, 1, '是', '启用', '1')
        elif f[0] in policy.LABELS.values():
            if raw not in ('开启','关闭','是','否','启用','停用',True,False,1,0,'1','0'):raise ValueError(f[0]+'须填写开启或关闭')
            key=next(k for k,v in policy.LABELS.items() if v==f[0])
            c=policy.patch(c,{key:raw in ('开启','是','启用',True,1,'1')});value['preset']['config']=c
        elif f[0] == 'parameters':
            if f[1] in FIXED:
                raise ValueError(schema.HELP[f[1]][0] + '不支持导入修改；周期请填各仓备货/到货天数，极端缺货边界固定30')
            n = number(raw)
            if c[f[0]][f[1]] != n:
                c[f[0]][f[1]] = n
        elif f[0] == 'warehouses':
            cycle = list(c['warehouses'].get(f[1], schema.standard()['warehouses'][f[1]]))
            n = number(raw)
            if cycle[f[2]] != n or f[1] not in c['warehouses']:
                cycle[f[2]] = n
                c['warehouses'][f[1]] = cycle
        elif f[0] == 'weight_profiles':
            if f[1] not in c['weight_profiles']:
                raise ValueError('门店方案没有此权重组：' + f[1] + '；请先在完整参数编辑中创建')
            n = number(raw)
            if c[f[0]][f[1]][f[2]] != n:
                c[f[0]][f[1]][f[2]] = n
        elif f[0] == 'category_profiles':
            if raw == '清空':
                c[f[0]].pop(f[1], None)
            else:
                c[f[0]][f[1]] = raw
        elif f[0] == 'profile_parameters':
            if f[1] not in c['weight_profiles']:raise ValueError('门店方案没有此分类方案：'+f[1])
            if raw=='沿用门店':
                group=c.get('profile_parameters',{}).get(f[1],{})
                group.pop(f[2],None)
                if not group:c.get('profile_parameters',{}).pop(f[1],None)
                if not c.get('profile_parameters'):c.pop('profile_parameters',None)
            else:
                n=number(raw)
                group=c.setdefault('profile_parameters',{}).setdefault(f[1],{})
                if group.get(f[2])!=n:group[f[2]]=n
    if ('预算金额',) in row['values'] and ('预算方式',) in row['values'] and ui['mode'] != 2:
        raise ValueError('填写预算金额时，预算方式须为限定预算；不限预算/未指定请留空金额')
    if ui.get('mode') == 2:
        number(ui.get('amount'))
    elif ('预算方式',) in row['values']:
        ui['amount'] = ''
    schema.validate(c)
    # Validate routes using the same contract as manual store defaults.
    store_defaults.record(value['preset'], c, ui)
    if value != original:
        value['preset'].setdefault('base_config', copy.deepcopy(original['preset'].get('base_config', original['preset']['config'])))
    return value


def comparable(value):
    """Preset origin metadata and selection state are not editable spreadsheet fields."""
    return {'config': value['preset']['config'], 'ui': {k: value['ui'].get(k) for k in ('mode', 'amount', 'primary', 'secondary')}}


def changes(before, after):
    maps = []
    for value in (before, after):
        comparable_value = comparable(value)
        comparable_value['config'] = copy.deepcopy(comparable_value['config'])
        comparable_value['config'][policy.KEY]=policy.values(comparable_value['config'])
        comparable_value['config']['warehouses'] = {wh: {'备货天数': cycle[0], '到货天数': cycle[1]}
            for wh, cycle in comparable_value['config']['warehouses'].items()}
        maps.append(dict(flatten(comparable_value)))
    old, new = maps
    for key in dict.fromkeys([*old, *new]):
        a, b = old.get(key), new.get(key)
        if a != b:
            # JSON reload makes warehouse tuples into lists; that is not a change.
            if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)) and list(a) == list(b):
                continue
            label = display_path(key.removeprefix('config / '))
            label = {'ui / mode': '预算方式', 'ui / amount': '预算金额', 'ui / primary': '主仓', 'ui / secondary': '从仓'}.get(label, label)
            if key == 'ui / mode':
                a, b = [('未指定', '不限预算', '限定预算')[x] for x in (a, b)]
            if key.startswith('config / '+policy.KEY+' / '):a,b=['开启' if x else '关闭' for x in (a,b)]
            yield label, a, b


def plan(data, bases, *, current=False, names=None):
    values, rows, errors, changed = {}, [], [], []
    for row in data['rows']:
        code = row['code']
        try:
            if current and code not in bases:
                raise ValueError('本次作业未载入此门店；可选择仅保存门店默认')
            known = (names or {}).get(code)
            if row['name'] and known and known != code and row['name'] != known:
                raise ValueError('门店名称与编码不一致：当前为' + known)
            original = bases.get(code) or base()
            value = patch(row, original)
            delta = list(changes(original, value))
            if delta or code not in bases:
                changed.append(code)
            values[code] = value
            for label, a, b in delta:
                rows.append([code, row['name'] or known or '', label, a, b, row['source']])
            if not delta:
                rows.append([code, row['name'] or known or '', '新增门店默认' if code not in bases else '无参数变化', '', '', row['source']])
        except (ValueError, TypeError) as exc:
            errors.append(row['source'] + ' / ' + code + '：' + str(exc))
    return {'values': values, 'rows': rows, 'errors': errors, 'changed': changed}


def write(path, records=None, progress=lambda _: None, cancel=None):
    """Template or editable export, using the existing application's Excel library."""
    records = records or {}
    headers = list(MINIMUM)
    for key in schema.HELP:
        if key not in FIXED:
            headers.append(schema.HELP[key][0])
    configs = [r['preset']['config'] for r in records.values()] if records else [schema.standard()]
    if not records:
        for name in schema.standard()['weight_profiles']:
            for key in schema.PROFILE_KEYS:headers.append('分类参数/'+name+'/'+schema.HELP[key][0])
    for config in configs:
        for name,values in config.get('profile_parameters',{}).items():
            for key in values:
                h='分类参数/'+name+'/'+schema.HELP[key][0]
                if h not in headers:headers.append(h)
        for name in config['weight_profiles']:
            for label in WEIGHTS.values():
                h = '权重/' + name + '/' + label
                if h not in headers:
                    headers.append(h)
        for name in config['category_profiles']:
            h = '分类映射/' + name
            if h not in headers:
                headers.append(h)
    wb = Workbook()
    ws = wb.active
    ws.title = '门店参数'
    ws.append(headers)
    ws.freeze_panes = 'C2'
    for code, record in records.items():
        checkpoint(cancel)
        c, ui = record['preset']['config'], record['ui']
        mapping = {'门店编码': code, '门店名称': record.get('name', ''), '主仓': ui.get('primary') or '未指定',
                   '从仓': ui.get('secondary') or '无从仓', '预算方式': ('未指定', '不限预算', '限定预算')[ui.get('mode', 0)],
                   '预算金额': number(ui.get('amount')) if ui.get('mode') == 2 else None, '启用一箱例外': '是' if c['exception'] else '否'}
        mapping.update({policy.LABELS[k]:'开启' if v else '关闭' for k,v in policy.values(c).items()})
        for h in headers[len(COMMON):]:
            f = field(h)
            v = c
            try:
                for part in f:
                    v = v[part]
            except KeyError:
                v = None
            mapping[h] = v
        ws.append([mapping.get(h) for h in headers])
    for cell in ws[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='126C63')
        cell.alignment = Alignment(wrap_text=True,vertical='center')
        ws.column_dimensions[cell.column_letter].width = 24
    ws.row_dimensions[1].height=72
    for row in ws:
        for cell in row:
            if isinstance(cell.value, str):
                cell.data_type = 's'
    for rn in range(2, max(ws.max_row + 1, 202)):
        ws.cell(rn, 1).number_format = '@'
    note = wb.create_sheet('填写说明')
    for pair in [('事项', '说明'), ('必填', '门店编码；其他字段选填。完整模板列出全部可编辑项，可删除不修改的列。按表头识别，可换列顺序。'),
                 ('空白与0', '空白保持原值；0是实际值。每店只能出现一行，多文件重复编码整批阻止。'),
                 ('补货策略', '极端缺货自动恢复、新品铺设填开启/关闭；空白保留，旧配置默认为开启。两仓共用；新品开启仍须选品。'),
                 ('生效范围', '预览后选择仅保存默认、仅应用本次、或应用并保存。历史签发不变。'),
                 ('预算', '填写预算金额自动选择限定预算；0元就是不安排。不限预算或未指定时金额留空。'),
                 ('主从仓', '主仓/从仓填河北仓或山东仓；清除从仓填无从仓；空白不清除。'),
                 ('权重', '权重/组名/趋势敏感度、最近7天权重、此前7天权重；w3自动计算，w1+w2不能超过1。'),
                 ('分类参数', '分类参数/组名/参数名：分类值优先于门店通用值，两仓共用。空白保持，填沿用门店清除该分类覆盖。无分类跟随快销。'),
                 ('分类映射', '分类映射/分类名：填方案中已有的权重组，填清空删除映射；空白保持原值。'),
                 ('固定项', '通用备货深度/到货时间不可导入修改，请设置各仓周期；极端缺货边界固定30。'),
                 ('数字与编码', '编码按文本保存以保留前导零及长编码；参数填非负数字，概率填0.50～0.99。禁止公式。'),
                 ('新增门店', '可按编码预存默认；其空白字段使用标准方案。名称可选，有已知名称时须一致。')]:
        note.append(pair)
    for key, (name, unit, help_text) in schema.HELP.items():
        if key not in FIXED:
            note.append((name + '（' + unit + '）', help_text))
    for name in schema.standard()['weight_profiles']:
        for key in schema.PROFILE_KEYS:note.append(('分类参数/'+name+'/'+schema.HELP[key][0],schema.HELP[key][2]+' 分类覆盖优先于门店通用值；两仓共用。'))
    note.column_dimensions['A'].width = 35
    note.column_dimensions['B'].width = 110
    path = Path(path)
    fd, temp = tempfile.mkstemp(dir=path.parent, suffix='.xlsx')
    os.close(fd)
    try:
        progress('正在保存门店参数Excel…')
        wb.save(temp)
        checkpoint(cancel)
        os.replace(temp, path)
    finally:
        wb.close()
        Path(temp).unlink(missing_ok=True)
    return str(path)
