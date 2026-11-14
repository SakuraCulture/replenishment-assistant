"""Explicit choices for contradictory rows; never guess or add quantities."""
import copy
import hashlib
import work_repository as wr


FACTS = ('quantity', 'unit', 'factor', 'conversion_basis', 'base_quantity',
         'amount', 'currency', 'note', 'name')
TITLES = dict(zip(FACTS, ('数量', '数量单位', '换算系数', '换算依据', '折合数量',
                         '金额', '币种', '备注', '商品名称')))


def candidate_id(row):
    # Include identity, original cells and file digest. A prior choice cannot be
    # silently applied to a changed source, mapping, or order association.
    return hashlib.sha256(wr.dumps(row).encode('utf-8')).hexdigest()


def find(rows):
    groups = {}
    for row in rows:
        identity = (row.get('signed_number'), row.get('revision'), row.get('store'),
                    row.get('warehouse'), row.get('document_number'), row.get('document_date'), row['sku'])
        groups.setdefault(identity, []).append(row)
    result = []
    for values in groups.values():
        if len(values) < 2:
            continue
        fields = [key for key in FACTS if any(wr.dumps(r.get(key)) != wr.dumps(values[0].get(key)) for r in values[1:])]
        if fields:
            token = hashlib.sha256(wr.dumps([candidate_id(r) for r in values]).encode('utf-8')).hexdigest()
            result.append({'sku': values[0]['sku'], 'fields': fields, 'rows': values, 'token': token})
    return result


def source_text(row):
    source = row.get('source') or {}
    return (str(source.get('file') or '来源文件未标明') + ' / ' +
            str(source.get('sheet') or '工作表未标明') + ' / 第' + str(source.get('row') or '?') + '行')


def display(value):
    return '空白（未填写）' if value is None else str(value)


def message(conflicts):
    lines = [f'同仓单同商品内容冲突（共{len(conflicts)}款）；请在“核对重复商品”中选择每款采用的原始明细，再继续核对。']
    for conflict in conflicts:
        lines.append('商品编码：' + conflict['sku'] + '；差异字段：' + '、'.join(TITLES[k] for k in conflict['fields']))
        for row in conflict['rows']:
            values = '；'.join(TITLES[k] + '=' + display(row.get(k)) for k in dict.fromkeys(('quantity', *conflict['fields'])))
            lines.append('  ' + source_text(row) + '；' + values)
    lines.append('数量不自动累加，明确填0不被正数覆盖；原文件保持不变，本次尚未保存单据。')
    return '\n'.join(lines)


def resolve(document, choices):
    """Return a new document with one explicitly selected whole row per conflict.

    Non-conflicting duplicates retain their existing deduplication contract.
    Resolution evidence is deterministic so repeat imports remain idempotent.
    """
    conflicts = find(document['scan']['rows'])
    if set(choices) != {c['token'] for c in conflicts}:
        raise ValueError('重复商品尚未全部选择，或原始明细已变化；请重新核对全部冲突，未保存单据。')
    replacements = {}
    for conflict in conflicts:
        candidates = {candidate_id(row): row for row in conflict['rows']}
        selected = candidates.get(choices[conflict['token']])
        if selected is None:
            raise ValueError('所选原始明细已变化，请重新核对重复商品；未保存单据。')
        winner = copy.deepcopy(selected)
        winner['duplicate_resolution'] = {
            'rule': 'explicit-source-row-v1', 'conflict_token': conflict['token'],
            'selected_id': choices[conflict['token']], 'selected_source': copy.deepcopy(selected['source']),
            'candidates': [{'id': candidate_id(row), 'source': copy.deepcopy(row['source']),
                            'facts': {k: copy.deepcopy(row.get(k)) for k in (*FACTS, 'quantity_origin')}}
                           for row in conflict['rows']],
        }
        for row in conflict['rows']:
            replacements[id(row)] = (conflict['token'], winner)
    result = {**document, 'scan': {**document['scan'], 'rows': []}}
    used = set()
    for row in document['scan']['rows']:
        replacement = replacements.get(id(row))
        if replacement:
            token, winner = replacement
            if token in used:
                continue
            used.add(token)
            result['scan']['rows'].append(winner)
        else:
            result['scan']['rows'].append(copy.deepcopy(row))
    return result


def document_context(document, number=None):
    scan = document['scan']
    files = list(dict.fromkeys(f['name'] for f in scan['files']))
    numbers = list(dict.fromkeys(r.get('signed_number') or (document.get('target') or {}).get('number') or '未明确' for r in scan['rows']))
    return '实际出错来源：' + '、'.join(files) + '\n实际订单：' + (number or '、'.join(numbers))


def audit_rows(rows):
    result = []
    for row in rows:
        audit = row.get('duplicate_resolution')
        if not audit:
            continue
        for candidate in audit['candidates']:
            source, facts = candidate['source'], candidate['facts']
            result.append([row['sku'], '已采用' if candidate['id'] == audit['selected_id'] else '未采用',
                           source['file'], source['sheet'], source['row'], facts['quantity'], facts['unit'],
                           facts['amount'], facts['currency']])
    return result
