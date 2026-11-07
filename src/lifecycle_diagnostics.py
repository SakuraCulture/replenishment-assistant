"""Explain existing whole-order association constraints without guessing identity."""


def binding(document, heads, *, scoped=False):
    rows = document['scan']['rows']
    candidates, rejected = [], []
    for head in heads:
        reason = ''
        for row in rows:
            for field, title in (('signed_number', '补货单号'), ('store', '门店编码'), ('warehouse', '仓')):
                target = 'number' if field == 'signed_number' else field
                if row.get(field) and row[field] != head[target]:
                    reason = title + '不一致'
                    break
            if reason:
                break
            references = row.get('identity_candidates') or []
            reference = row.get('identity_reference')
            if reference:
                references = [reference]
            if references:
                matched = [r for r in references if r['number'] == head['number']]
                if not matched:
                    reason = '不在文件的原签发候选中'
                elif not any(all(r.get(k) == head.get(k) for k in ('number', 'store', 'day', 'revision', 'digest')) for r in matched):
                    reason = '原签发已变化：文件修订 ' + ' / '.join('V%03d' % r['revision'] for r in matched) + '，当前 V%03d；旧回传不能套用新签发' % head['revision']
            elif row.get('revision') and row['revision'] != head['revision']:
                reason = '文件修订与当前签发修订不一致'
            if reason:
                break
        if reason:
            rejected.append({'head': head, 'reason': reason})
        else:
            candidates.append(head)
    numbers = {r.get('signed_number') for r in rows if r.get('signed_number')}
    explicit = len(numbers) == 1 and all(r.get('signed_number') for r in rows)
    automatic = len(candidates) == 1 and (explicit or scoped and len(heads) == 1)
    if not candidates:
        related = [x for x in rejected if x['head']['number'] in numbers]
        reason = related[0]['reason'] if related else ('明确单号未匹配当前范围内有效仓单；请核对数据目录、删除状态及所选范围' if numbers else '当前范围没有匹配的签发仓单；请核对门店、仓和签发记录')
        state = '无法绑定'
    elif automatic:
        state = '已匹配原单' if explicit else '按已选单据范围绑定'
        reason = '已匹配补货单号、门店和仓；下一步仍核对修订、数量及原文件。'
    else:
        state = '需要明确关联'
        reason = ('文件未提供唯一原单身份' if not explicit else '找到多个候选') + f'；可选 {len(candidates)} 张，请核对业务日期、店仓和修订，不能按商品相似度猜单。'
    return dict(candidates=candidates, rejected=rejected, automatic=automatic, state=state, reason=reason)


def issue(message):
    if any(word in message for word in ('隐藏', '关联信息', '冲突')):
        return '文件中的订单身份依据损坏或冲突。请核对原签发文件；不能忽略标记后猜测绑定。'
    if '缓存' in message:
        return '公式结果未保存。请在 Excel/WPS 中重新计算并保存副本，再读取。'
    if '映射' in message or '表头' in message or '工作表' in message:
        return '文件结构未识别。请核对业务工作表和字段映射；不确定的部分先保留待处理。'
    return '尚未读取成功，未保存回传。请查看完整原因并核对文件。'
