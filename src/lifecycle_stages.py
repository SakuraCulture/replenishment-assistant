"""Document routes and warehouse-order progress shared by lifecycle readers."""

LEGACY = ('return', 'payment', 'receipt')
CURRENT = ('return', 'operations', 'payment', 'receipt')
PENDING = {
    'return': '待回传',
    'operations': '待运营签发',
    'payment': '待付款',
    'receipt': '待收货',
}
FINISHED = '已收齐'


def route(operations_required):
    return CURRENT if operations_required else LEGACY
