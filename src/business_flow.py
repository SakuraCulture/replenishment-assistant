"""Read-only business navigation projections. No quantities or persisted states."""
from runtime_support import checkpoint


def needs_attention(row):
    task = row.get('generation_task') or {}
    changed = any(row.get('present', {}).get(s) and not row.get('health', {}).get(s, True)
                  for s in ('return', 'payment', 'receipt'))
    return bool(changed or row.get('legacy_incomplete') or row.get('old_revision')
                or row.get('warehouse') == '旧版未标仓'
                or task.get('status') in ('failed', 'blocked', 'needs_review'))


def home_projection(records, batches):
    groups = {s: [] for s in ('return', 'payment', 'receipt', 'attention')}
    for row in records:
        stage = {'jst_payment': 'payment', 'jst_generate': 'payment'}.get(row.get('next'), row.get('next'))
        if stage in groups:
            groups[stage].append(row)
        if needs_attention(row):
            groups['attention'].append(row)
    active = next((b for b in batches if b['status'] in ('active', 'paused')
                   and sum(b['counts'].get(s, 0) for s in ('pending', 'failed'))), None)
    return {'groups': groups, 'latest_batch': active, 'total': len(records)}


def read_home(path, cache, cancel=None, progress=lambda _: None):
    import daily_batch
    import jst_batches
    data = daily_batch.read(path, cache, {'all_matched': True}, cancel, progress)
    checkpoint(cancel)
    return home_projection(data['records'], jst_batches.listing(path, cancel=cancel))


def empty_message(stage, request, data):
    """Explain visible scope without guessing global counts or payment facts."""
    name = {'return': '回传', 'payment': '付款', 'receipt': '收货'}.get(stage, '订单')
    scope = request.get('scope')
    if scope is not None:
        return f'限定所选 {len(scope)} 单，当前没有待处理的{name}记录。可查看本环节全部；进入其他环节仍保留这些订单。'
    if not data.get('scope'):
        return '当前日期和搜索范围没有签发单。请调整筛选；尚未签发的门店请先完成补货出单。'
    if request.get('warehouse') or request.get('lock'):
        return f'当前仓或锁状态下没有适用{name}记录。可清空筛选查看；不会修改订单或自动生成单据。'
    if stage and request.get('stage_filter') == '待处理':
        upstream = {'return': '签发', 'payment': '门店回传', 'receipt': '门店付款'}[stage]
        return f'当前范围没有待{name}订单。查看本环节全部可查已处理记录；尚未到本环节的订单，请先核对{upstream}。'
    return '当前条件没有适用记录。可清空筛选或返回订单跟踪，不代表订单已完成。'


def review_state(state, result):
    """Presentation-only; budget shortages are not blockers."""
    if state in ('计算失败', '待重算'):
        return 'blocking'
    if state != '已完成' or not result:
        return 'pending'
    s = result['summary']
    if s.get('issues') or s.get('unpriced') or s.get('holding_gaps'):
        return 'review'
    return 'ready'
