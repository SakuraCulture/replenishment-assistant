"""Read-only lifecycle ranges over the existing signed warehouse-order progress."""

OPTIONS = (
    ('', '全部已签发'),
    ('operations', '待运营签发及以后'),
    ('payment', '待付款及以后'),
    ('receipt', '待收货及以后'),
    ('finished', '已结束'),
)
LABELS = dict(OPTIONS)
_RANK = {'return': 0, 'operations': 1, 'payment': 2, 'receipt': 3}


def validate(value):
    if not isinstance(value, str) or value not in LABELS:
        raise ValueError('未知生命周期范围')
    return value


def includes(view, value=''):
    """Entering a pending stage qualifies; confirmation of it is not required."""
    validate(value)
    if not value:
        return True
    next_stage = view['next']
    if value == 'finished':
        return next_stage is None
    if next_stage in _RANK:
        return _RANK[next_stage] >= _RANK[value]
    # A zero-demand return can end a signed order before payment ever starts.
    # Completed later stages qualify, but an early end is not a later stage.
    health = view['health']
    return any(health.get(stage, False) for stage in _RANK if _RANK[stage] >= _RANK[value])


def label(query):
    return LABELS[validate(query.get('lifecycle_from', ''))]
