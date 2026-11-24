"""Read, review and publish the ordered store lists used by replenishment plans."""
from collections import defaultdict
from pathlib import Path

from openpyxl import load_workbook

import import_templates
import parameter_presets as presets
import parameter_schema
import replenishment_plans as plans
from runtime_support import checkpoint
from store_parameter_import import code_value, digest, number


KIND = 'replenishment_plans'
_, SHEET, REQUIRED, OPTIONAL = import_templates.TEMPLATES[KIND]
FOLLOW = '跟随门店默认'
FIXED = '原有固定配置（兼容保留）'


def _text(value):
    return '' if value is None else str(value).strip()


def read(paths, progress=lambda _: None, cancel=None):
    groups, sources = {}, {}
    for path in dict.fromkeys(Path(p).resolve() for p in paths):
        checkpoint(cancel)
        progress('读取补货计划 ' + path.name)
        checksum = digest(path)
        book = load_workbook(path, read_only=True, data_only=False, keep_links=False)
        try:
            if SHEET not in book.sheetnames:
                raise ValueError(f'{path.name} 缺少「{SHEET}」工作表，请使用补货计划模板')
            sheet = book[SHEET]
            sheet.reset_dimensions()
            iterator = iter(sheet.iter_rows())
            headers = [_text(cell.value) for cell in next(iterator, ())]
            fields = [header for header in headers if header]
            unknown = set(fields) - set(REQUIRED + OPTIONAL)
            if unknown:
                raise ValueError(f'{path.name} 有无法导入的列 {"、".join(sorted(unknown))}')
            if len(fields) != len(set(fields)) or any(h not in fields for h in REQUIRED):
                raise ValueError(f'{path.name} 的计划表头重复或缺少必填列（{"、".join(REQUIRED)}）')
            for row_number, cells in enumerate(iterator, 2):
                checkpoint(cancel)
                if all(cell.value in (None, '') for cell in cells):
                    continue
                source = f'{path.name} / 第{row_number}行'
                try:
                    if any(cell.data_type in ('f', 'e') for cell in cells):
                        raise ValueError('请粘贴为值，计划表不能使用公式或 Excel 错误值')
                    if any(cell.value not in (None, '') and (i >= len(headers) or not headers[i])
                           for i, cell in enumerate(cells)):
                        raise ValueError('数据列缺少表头')
                    row = {h: cells[i].value for i, h in enumerate(headers) if h and i < len(cells)}
                    plan_name = _text(row.get('计划名称'))
                    if not plan_name:
                        raise ValueError('请填写计划名称')
                    index = headers.index('门店编码')
                    if index >= len(cells):
                        raise ValueError('请填写门店编码')
                    store = code_value(cells[index])
                    order = _text(row.get('顺序'))
                    order = number(order) if order else None
                    if order is not None and (order < 1 or order != int(order)):
                        raise ValueError('顺序须为正整数')
                    groups.setdefault(plan_name, []).append({
                        'code': store, 'name': _text(row.get('门店名称')), 'order': order,
                        'preset': _text(row.get('参数预案')), 'note': _text(row.get('计划备注')),
                        'source': source,
                    })
                except ValueError as exc:
                    raise ValueError(f'{source}：{exc}') from exc
        finally:
            book.close()
        if digest(path) != checksum:
            raise ValueError(f'{path.name} 在读取中发生变化，请保存后重试')
        sources[str(path)] = checksum
    if not groups:
        raise ValueError('计划表没有填写门店名单')
    return {'groups': groups, 'sources': sources}


def _ordered_rows(name, rows):
    seen = set()
    for row in rows:
        if row['code'] in seen:
            raise ValueError(f"{row['source']}：计划「{name}」内门店编码重复 {row['code']}")
        seen.add(row['code'])
    orders = [row['order'] for row in rows]
    if all(order is None for order in orders):
        return rows
    if None in orders or len(orders) != len(set(orders)):
        raise ValueError(f'计划「{name}」的顺序须全部留空，或逐行填写不同的正整数')
    return sorted(rows, key=lambda row: row['order'])


def _store(row, known, prior, choices, mode):
    code = row['code']
    known_name = known.get(code) or prior.get('name')
    if not known_name or known_name == code:
        known_name = row['name'] or known_name
    if not known_name:
        raise ValueError(f'{code} 未在门店目录登记，请核对编码并填写门店名称')
    if row['name'] and row['name'] != known_name:
        raise ValueError(f'{code} 的门店名称与登记不符，当前为「{known_name}」')
    value = {**prior, 'code': code, 'name': known_name}
    choice = row['preset']
    if mode != presets.MODE:
        if choice:
            raise ValueError('该计划仍使用旧参数模式，请先在界面转换为逐店选择参数')
        return value
    if not choice:
        value.setdefault('preset_id', None)
    elif choice == FOLLOW:
        value['preset_id'] = None
    elif choice == FIXED and prior.get('preset_id') == 'standard':
        value['preset_id'] = 'standard'
    else:
        ids = choices.get(choice, ())
        if len(ids) != 1:
            reason = '存在重名' if ids else '未找到或已经停用'
            raise ValueError(f'参数预案「{choice}」{reason}，请核对后再导入')
        value['preset_id'] = ids[0]
    return value


def _choice_label(item, active):
    key = item.get('preset_id')
    if key == 'standard':
        return FIXED
    if key:
        return active.get(key, {}).get('name') or '原预案待核对'
    return FOLLOW


def prepare(data, snapshot, names):
    by_name, choices = defaultdict(list), defaultdict(list)
    active = presets.visible(snapshot)
    for value in snapshot['plans']:
        by_name[_text(value['name'])].append(value)
    for item in active.values():
        choices[item['name']].append(item['id'])

    edits, review, unchanged = [], [], 0
    for name, group in data['groups'].items():
        matches = by_name.get(name, ())
        if len(matches) > 1:
            raise ValueError(f'已有多份同名补货计划「{name}」，请在计划库更名后再导入')
        old = matches[0] if matches else None
        base = old or {'name': name, 'parameter_mode': presets.MODE, 'config': parameter_schema.standard()}
        before = {item['code']: (i, item) for i, item in enumerate(base.get('stores', []), 1)}
        note = {r['note'] for r in group if r['note']}
        if len(note) > 1:
            raise ValueError(f'计划「{name}」有不同的备注，请核对同一计划的备注')
        stores = []
        for row in _ordered_rows(name, group):
            try:
                prior = before.get(row['code'], (None, {}))[1]
                stores.append(_store(row, names, prior, choices, base.get('parameter_mode')))
            except ValueError as exc:
                raise ValueError(f"{row['source']}：{exc}") from exc
        value = {**base, 'stores': stores, 'note': next(iter(note), base.get('note', ''))}
        plans.validate(value)
        if old and stores == old['stores'] and value['note'] == old.get('note', ''):
            unchanged += 1
            continue
        edits.append((value, old))
        previous_note = base.get('note', '')
        note_change = (f'{previous_note} → {value["note"]}' if old and previous_note != value['note']
                       else value['note'])
        review.append([name, '更新计划' if old else '新建计划', '', f'{len(before)} → {len(stores)} 家', '', note_change])
        retained = {item['code'] for item in stores}
        for index, store in enumerate(stores, 1):
            old_index, old_store = before.get(store['code'], ('', {}))
            params = _choice_label(store, active)
            previous = _choice_label(old_store, active)
            if old_store and previous != params:
                params = f'{previous} → {params}'
            action = '保留'
            if not old_store:
                action = '加入'
            elif old_index != index or old_store != store:
                action = '调整'
            review.append([name, action, old_index, index,
                           f"{store['name']} · {store['code']}",
                           params if base.get('parameter_mode') == presets.MODE else '沿用旧计划参数'])
        for code, (index, item) in before.items():
            if code not in retained:
                review.append([name, '移出', index, '', f"{item['name']} · {code}", ''])
    return {'edits': edits, 'rows': review, 'unchanged': unchanged}


def save(path, data, prepared, snapshot, progress=lambda _: None, cancel=None):
    for source, expected in data['sources'].items():
        checkpoint(cancel)
        if digest(source) != expected:
            raise ValueError(f'{Path(source).name} 在预览后发生变化，请重新导入')
    checkpoint(cancel)
    progress('保存已核对的补货计划')
    return plans.Repository(path).save_batch(prepared['edits'], expected_snapshot=snapshot)


def export(path, snapshot, progress=lambda _: None, cancel=None):
    active = presets.visible(snapshot)
    plan_names = set()
    preset_counts = defaultdict(int)
    for preset in active.values():
        preset_counts[preset['name']] += 1
    values = []
    for plan in snapshot['plans']:
        checkpoint(cancel)
        name = plan['name']
        if _text(name) in plan_names:
            raise ValueError(f'存在同名补货计划「{name}」，请更名后再导出可回导的计划表')
        plan_names.add(_text(name))
        for order, store in enumerate(plan['stores'], 1):
            choice = ''
            if plan.get('parameter_mode') == presets.MODE:
                key = store.get('preset_id')
                choice = _choice_label(store, active)
                if key not in (None, 'standard') and (key not in active or preset_counts[choice] != 1):
                    raise ValueError(f"计划「{name}」的门店 {store['code']} 选择的参数预案待核对")
            values.append([name, store['code'], store['name'], order, choice, plan.get('note', '') if order == 1 else ''])
    if not values:
        raise ValueError('暂无可导出的补货计划')
    progress('写入补货计划表')
    book = import_templates.make_workbook(KIND, blank_rows=len(values))
    sheet = book[SHEET]
    for row_index, row in enumerate(values, 2):
        for column, value in enumerate(row, 1):
            cell = sheet.cell(row_index, column, value)
            if isinstance(value, str):
                cell.data_type = 's'
    return import_templates.save_workbook(book, path)
